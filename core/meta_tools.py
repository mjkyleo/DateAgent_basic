"""元数据探查工具 MetaTool（设计文档 §9.2）。

定位：只提供元信息，**不做任何业务分析**。对适配器的薄封装 + 有界约束：
- 表数量、列数量、采样行数全部封顶，防止元数据接口本身成为慢查询源；
- 输出结构化 Schema 文本（给 LLM 当上下文）与结构化 JSON（给前端树）。

两层次的事实，别混着看：
- **结构事实**（table_schema / list_tables / indexes）：来自 information_schema，便宜；
- **内容事实**（table_stats / content_facts）：要真的聚合数据，贵且有隐私面，
  所以每一处都有预算（表数、列数、高频值个数、行数上限），并且失败一律降级，
  绝不允许它把提问链路带崩。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..config.settings import Settings
from ..utils.cache import cache_get, cache_set
from ..utils.logging import get_logger
from .data_source.base import DataSourceAdapter

log = get_logger("basic.core.meta")

MAX_TABLES = 200          # 单库表清单上限
MAX_COLUMNS = 60          # 单表列上限
MAX_DISTINCT_TOPN = 20    # 计算频次分布的 distinct 上限

# ---- 内容画像预算（进 LLM prompt 的那条路径专用）----
# 每画像一张表 = 1 次 COUNT(*) + 每列 1 次聚合 + 低基数列 1 次 GROUP BY。
# 意图解析是用户交互式提问的一环，20 张表 × 60 列会打出上千条聚合 SQL，
# 所以这里按"提问路径"而不是"完整性"来定预算：宁可少讲几张表。
PROMPT_MAX_TABLES = 6         # 进 prompt 的表数上限
PROMPT_MAX_COLUMNS = 12       # 每表取前 N 列做画像（列太多时后面的意义递减）
PROMPT_MAX_TOP_VALUES = 5     # 每列送出的高频取值数
PROMPT_MAX_ROWS = 2_000_000   # 超过这个行数的表不做列级聚合（COUNT(DISTINCT) 会拖死）
NUMERIC_AS_DIM_MAX = 10       # 数值列只有这么少的取值才算维度（年份/星级/城市编码）

# 命中即"只报统计、不报取值"：min/max/top_values 都是真实数据内容，
# 联系方式与证件号这类列一旦进 prompt 就等于把明文发给了模型服务商。
SENSITIVE_RE = re.compile(
    r"(phone|mobile|tel|email|mail|id_?card|passport|ssn|address|token|password"
    r"|secret|姓名|电话|手机|邮箱|邮件|身份证|地址|证件)", re.I)

_NUMERIC_RE = re.compile(r"(int|decimal|numeric|float|double|real|money)", re.I)
_TIME_RE = re.compile(r"(date|time|timestamp)", re.I)


class MetaTool:
    def __init__(self, adapter: DataSourceAdapter, settings: Settings,
                 cache_scope: str = "") -> None:
        """cache_scope：数据源标识（如 `ds3`）。

        表清单会被短时缓存（META_CACHE_TTL_S，默认 30s），必须带上数据源维度，
        否则切了数据源会拿到上一个库的表名——Schema 校验就形同虚设了。
        留空表示不缓存（测试/一次性探查）。
        """
        self.adapter = adapter
        self.settings = settings
        self._cache_scope = cache_scope

    def close(self) -> None:
        """释放适配器持有的连接。

        必须调用：否则每次请求都留一个连接（MySQL 会耗尽连接数；
        SQLite 的 StaticPool 会一直锁住文件，导致删除数据源时删不掉 .db）。
        """
        try:
            self.adapter.close()
        except Exception:  # noqa: BLE001  关闭失败不影响已取到的结果
            pass

    def __enter__(self) -> "MetaTool":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------ 库 / 表
    def list_databases(self) -> List[str]:
        return self.adapter.list_databases()

    @property
    def qualifies_tables(self) -> bool:
        """SQL 里要不要写 `库`.`表`（取决于数据源，见 DataSourceAdapter.qualifies_tables）。"""
        return bool(getattr(self.adapter, "qualifies_tables", True))

    def qualified_name(self, db_name: str, table_name: str) -> str:
        """把 (库, 表) 拼成该数据源能识别的引用形式（交给适配器决定）。"""
        return self.adapter.qualified_name(db_name, table_name)

    def list_tables(self, db_name: str) -> List[str]:
        """表清单（封顶 MAX_TABLES）。一轮链路内会被短时缓存，少打 information_schema。

        只缓存**成功**的结果：查询失败不缓存，异常照样往上抛 —— 缓存层不能改变
        调用方的失败语义（`parse_intent`/Schema 校验依赖这个异常做降级）。
        """
        key = f"{self._cache_scope}:tables:{db_name}" if self._cache_scope else ""
        ttl = float(getattr(self.settings, "meta_cache_ttl_s", 0) or 0)
        if key and ttl > 0:
            ok, cached = cache_get(key, ttl)
            if ok:
                return cached
        tables = self.adapter.list_tables(db_name)[:MAX_TABLES]
        if key and ttl > 0:
            cache_set(key, tables, ttl)
        return tables

    # ------------------------------------------------------------ 结构 / 采样
    def table_schema(self, db_name: str, table_name: str) -> Dict[str, Any]:
        cols = self.adapter.get_table_schema(db_name, table_name)[:MAX_COLUMNS]
        return {"database": db_name, "table": table_name, "columns": cols,
                "row_estimate": None}

    def sample(self, db_name: str, table_name: str,
               limit: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.adapter.sample_data(
            db_name, table_name, limit or self.settings.max_sample_rows)

    # ------------------------------------------------------------ 分布统计
    def table_stats(self, db_name: str, table_name: str, *,
                    max_cols: Optional[int] = None,
                    top_limit: int = 10,
                    use_cache: bool = False) -> Dict[str, Any]:
        """字段级分布：空值数、distinct、min/max/avg；低基数列附 Top 频次。

        单条聚合 SQL 拿全表指标（有界：不拉原始行）。

        注意连接复用：早前每列（甚至每个频次查询）都 `engine.connect()` 一次，
        60 列的表要开上百次连接 —— 池子被反复借还，慢且容易把 MySQL 连接数打满。
        这里改成整张表一次连接、循环复用。

        max_cols / top_limit 是给 prompt 路径的预算（前端整表视图不传，照旧全列）；
        use_cache 只在带数据源作用域时生效，TTL 与表清单一致。
        """
        col_cap = min(max_cols, MAX_COLUMNS) if max_cols else MAX_COLUMNS
        key = (f"{self._cache_scope}:stats:{db_name}.{table_name}:{col_cap}:{top_limit}"
               if (use_cache and self._cache_scope) else "")
        ttl = float(getattr(self.settings, "meta_cache_ttl_s", 0) or 0)
        if key and ttl > 0:
            ok, cached = cache_get(key, ttl)
            if ok:
                return cached
        stats = self._collect_stats(db_name, table_name, col_cap, top_limit)
        if key and ttl > 0:
            cache_set(key, stats, ttl)      # 只缓存成功结果（失败时上面已抛异常）
        return stats

    def _collect_stats(self, db_name: str, table_name: str,
                       col_cap: int, top_limit: int) -> Dict[str, Any]:
        cols = self.adapter.get_table_schema(db_name, table_name)[:col_cap]
        ref = self.adapter.qualified_name(db_name, table_name)
        col_stats: List[Dict[str, Any]] = []
        with _ctx(self.adapter) as conn:
            total = conn.execute(_q(f"SELECT COUNT(*) FROM {ref}")).fetchone()[0]
            for c in cols:
                name = c["name"]
                quoted = _q_ident(name)
                numeric = bool(_NUMERIC_RE.search(c.get("type") or ""))
                # 敏感列不做 min/max 与频次：那两类结果就是真实数据内容本身
                masked = bool(SENSITIVE_RE.search(name)) or bool(
                    SENSITIVE_RE.search(c.get("comment") or ""))
                # 逗号属于 agg 自己：漏掉会拼出 `MAX(x) AVG(x)`，整列聚合直接报错，
                # 于是所有数值列都变成"该列无法聚合统计"（度量清单永远为空）。
                agg = (f", AVG({quoted})" if numeric and not masked else ", NULL")
                try:
                    row = conn.execute(_q(
                        f"SELECT SUM(CASE WHEN {quoted} IS NULL THEN 1 ELSE 0 END),"
                        f" COUNT(DISTINCT {quoted}),"
                        f" MIN({quoted}), MAX({quoted}){agg} FROM {ref}")).fetchone()
                except Exception:  # noqa: BLE001  单列失败不拖垮整体（如 BLOB 列）
                    col_stats.append({"column": name, "error": "该列无法聚合统计"})
                    continue
                null_count = int(row[0] or 0)
                distinct = int(row[1]) if row[1] is not None else None
                stat: Dict[str, Any] = {
                    "column": name,
                    "null_count": null_count,
                    "null_ratio": round(null_count / total, 4) if total else 0.0,
                    "distinct_count": distinct,
                    "min_value": None if masked else _jsonable(row[2]),
                    "max_value": None if masked else _jsonable(row[3]),
                    "avg_value": _round_avg(row[4]),
                    "top_values": [] if masked else self._frequency(
                        conn, ref, name, top=top_limit)
                    if distinct is not None and 0 < distinct <= MAX_DISTINCT_TOPN
                    else [],
                }
                if masked:
                    stat["masked"] = True
                col_stats.append(stat)
        return {"database": db_name, "table": table_name,
                "row_count": int(total), "columns": col_stats}

    @staticmethod
    def _frequency(conn: Any, ref: str, column: str,
                   top: int = 10) -> List[Dict[str, Any]]:
        """低基数列的简单频次分布（GROUP BY + ORDER BY 计数，封顶 top）。

        排除 NULL：空值多少已经由 null_ratio 说了，把 None 混进"高频取值"里，
        模型会写出 `WHERE 城市 = 'None'` 这种查不到任何东西的条件。
        """
        quoted = _q_ident(column)
        rows = conn.execute(_q(
            f"SELECT {quoted} AS v, COUNT(*) AS c FROM {ref} "
            f"WHERE {quoted} IS NOT NULL "
            f"GROUP BY {quoted} ORDER BY c DESC LIMIT {int(top)}")).fetchall()
        return [{"value": _jsonable(r[0]), "count": int(r[1])} for r in rows]

    # ------------------------------------------------------------ 优化建议元数据
    def table_indexes(self, db_name: str, table_name: str) -> List[Dict[str, Any]]:
        """索引清单（带数据源维度缓存，与表清单同一套 TTL）。

        取不到时返回 [] 而不抛异常：优化建议在没有索引事实时会自动降级
        （只报 SQL 层面的问题），不该因为元数据缺失让整条建议失败。
        """
        key = f"{self._cache_scope}:idx:{db_name}.{table_name}" if self._cache_scope else ""
        ttl = float(getattr(self.settings, "meta_cache_ttl_s", 0) or 0)
        if key and ttl > 0:
            ok, cached = cache_get(key, ttl)
            if ok:
                return cached
        try:
            idx = self.adapter.table_indexes(db_name, table_name)
        except Exception as e:  # noqa: BLE001  同上：元数据缺失只降级，不阻断
            log.warning("取索引失败 %s.%s：%s", db_name, table_name,
                        type(e).__name__)
            idx = []
        if key and ttl > 0:
            cache_set(key, idx, ttl)
        return idx

    def table_row_estimate(self, db_name: str, table_name: str) -> Optional[int]:
        """行数估计（同样缓存）。拿不到返回 None，由调用方决定阈值判断是否跳过。"""
        key = f"{self._cache_scope}:rows:{db_name}.{table_name}" if self._cache_scope else ""
        ttl = float(getattr(self.settings, "meta_cache_ttl_s", 0) or 0)
        if key and ttl > 0:
            ok, cached = cache_get(key, ttl)
            if ok:
                return cached
        try:
            rows = self.adapter.table_row_estimate(db_name, table_name)
        except Exception:  # noqa: BLE001
            rows = None
        if key and ttl > 0:
            cache_set(key, rows, ttl)
        return rows

    # ------------------------------------------------------------ 内容画像（给 prompt）
    def content_facts(self, db_name: str, tables: List[str], *,
                      max_tables: int = PROMPT_MAX_TABLES,
                      max_columns: int = PROMPT_MAX_COLUMNS,
                      top_values: int = PROMPT_MAX_TOP_VALUES,
                      ) -> List[Dict[str, Any]]:
        """把前 N 张表聚合成"内容事实"结构，供 prompt 与第一步面板共用。

        与 table_stats 的区别全在预算与容错：这里每张表只取前 max_columns 列、
        每列最多 top_values 个取值，且**任何一表失败都只跳过该表** ——
        画像是增强信息，不该有让提问失败的能力。
        """
        out: List[Dict[str, Any]] = []
        for t in [x for x in (tables or []) if x][:max_tables]:
            try:
                est = self.table_row_estimate(db_name, t)
                if est is not None and est > PROMPT_MAX_ROWS:
                    # 列级聚合是 COUNT(DISTINCT)+GROUP BY，千万行表上会把提问路径拖死
                    out.append({"table": t, "row_count": est,
                                "skipped": f"表约 {est:,} 行，超过画像预算，未做列级统计"})
                    continue
                cols = self.adapter.get_table_schema(db_name, t)[:max_columns]
                stats = self.table_stats(db_name, t, max_cols=max_columns,
                                         top_limit=top_values, use_cache=True)
                out.append(self.table_fact(t, cols, stats))
            except Exception as e:  # noqa: BLE001  降级：这张表不进事实清单
                log.warning("内容画像跳过 %s.%s：%s", db_name, t, type(e).__name__)
        return out

    def table_fact(self, table: str, cols: List[Dict[str, Any]],
                   stats: Dict[str, Any]) -> Dict[str, Any]:
        """列结构 + 逐列聚合 → prompt 友好的四类事实（时间跨度/维度/度量/空值）。"""
        return _shape_fact(table, cols, stats)

    @staticmethod
    def facts_text(facts: List[Dict[str, Any]]) -> str:
        """内容事实 → prompt 文本。

        刻意不用 `- ` 开头的行：core.llm_intent 的规则兜底要把 '## 表 X' 之后
        任何 `- ` 行当成字段清单（_parse_schema_text），事实块用破折号列表会
        凭空多出几个"字段"，把防幻觉表校验带偏。
        """
        lines: List[str] = []
        for f in facts:
            head = f"表 {f['table']}"
            if f.get("row_count") is not None:
                head += f"｜{f['row_count']:,} 行"
            if f.get("skipped"):
                lines.append(f"{head}｜{f['skipped']}")
                continue
            tr = f.get("time_range")
            if tr:
                head += f"｜时间 {tr['start']} ~ {tr['end']}（列 {tr['column']}）"
            lines.append(head)
            for d in f.get("dimensions") or []:
                vals = "/".join(f"{v['value']}({v['count']})" for v in d["top"])
                lines.append(f"    维度 {d['column']}：{d['distinct']} 种取值"
                             + (f"，高频 {vals}" if vals else ""))
            for m in f.get("measures") or []:
                rng = f"{m['min']} ~ {m['max']}"
                if m.get("avg") is not None:
                    rng += f"，均值 {m['avg']}"
                lines.append(f"    度量 {m['column']}：{rng}")
            if f.get("null_heavy"):
                lines.append("    空值偏高：" + "、".join(
                    f"{n['column']} {n['ratio']:.0%}" for n in f["null_heavy"]))
            if f.get("masked"):
                lines.append("    含敏感列（只给统计不给取值）：" + "、".join(f["masked"]))
        return "\n".join(lines)

    # ------------------------------------------------------------ LLM 上下文
    def schema_context(self, db_name: str, tables: List[str],
                       fields: Optional[List[str]] = None,
                       with_facts: bool = True) -> str:
        """生成给 LLM 的 Schema 文本（选中的库表字段；无选中则全库前 N 张表）。

        with_facts=True 时附一段【数据内容事实】：行数、时间跨度、维度高频值、
        度量范围。没有这一段，模型只能凭列名猜"这张表到底装了什么"，
        生成的 SQL 与结论都停留在列名层面 —— 这正是"分析不讲现象"的根因。
        """
        # MySQL 要求写 `库`.`表` 全限定名：连接默认库未必等于 db_name，裸表名会查错库。
        # 单库来源（SQLite 文件）反过来 —— 写库限定名会变成 "no such table: db.table"。
        # 安全门取表名时用 sqlglot 的 Table.name（不含库前缀），两种写法都不影响存在性校验。
        ref_hint = (f"SQL 中表名写成 `{db_name}`.`表名` 的完整形式"
                    if self.qualifies_tables else "SQL 中直接写表名（`表名`），不要加库前缀")
        lines = [f"# 数据库 {db_name} 的表结构（只允许使用以下表与字段；{ref_hint}）"]
        targets = tables or self.list_tables(db_name)[:10]
        picked = targets[:20]
        for t in picked:
            try:
                cols = self.adapter.get_table_schema(db_name, t)[:MAX_COLUMNS]
            except Exception:  # noqa: BLE001  单表失败跳过，不阻塞上下文组装
                continue
            lines.append(f"\n## 表 {t}")
            for c in cols:
                mark = " (PK)" if c.get("primary_key") else ""
                # 注释跟在 " # " 之后：类型解析要在那里截断（见 llm_intent._parse_schema_types）
                comment = _one_line(c.get("comment"))
                note = f"  # {comment}" if comment else ""
                lines.append(f"  - {c['name']}: {c['type']}{mark}{note}")
        if with_facts:
            facts = self.content_facts(db_name, picked)
            body = self.facts_text(facts)
            if body:
                lines.append("\n# 【数据内容事实】（真实聚合结果，不是推测；"
                             "结论里出现的数字必须来自这里）")
                lines.append(body)
        return "\n".join(lines)


# ---------------------------------------------------------------- 内部助手
class _ctx:
    """适配器连接的薄上下文（MySQL 适配器经 SQLAlchemy engine 管理）。"""

    def __init__(self, adapter: DataSourceAdapter):
        self._adapter = adapter

    def __enter__(self):
        self._conn = self._adapter.engine.connect()
        return self._conn

    def __exit__(self, *exc):
        self._conn.close()
        return False


def _q(sql: str):
    from sqlalchemy import text
    return text(sql)


def _q_ident(name: str) -> str:
    """列名标识符：白名单校验后包反引号（MetaTool 内所有拼接都经此函数）。"""
    import re
    if not re.fullmatch(r"[A-Za-z0-9_$\u4e00-\u9fff]+", name or ""):
        raise ValueError(f"非法列名: {name!r}")
    return f"`{name}`"


def _jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _round_avg(v: Any) -> Any:
    """AVG 结果压成可读数字：浮点长尾（0.30000000000000004）进 prompt 只会浪费 token。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _jsonable(v)
    return int(f) if f.is_integer() else round(f, 4)


def _one_line(v: Any) -> str:
    """压成单行：列注释里带换行会把 Schema 文本的"一行一列"结构打断。"""
    return " ".join(str(v or "").split())


def _shape_fact(table: str, cols: List[Dict[str, Any]],
                stats: Dict[str, Any]) -> Dict[str, Any]:
    """把「列结构 + 逐列聚合」压成 prompt 友好的四类事实。

    分类靠类型而不是列名：中文列名（「订单额」「平台」）不含"金额/数量"字样，
    按名字猜会把 SUM(平台) 这种荒谬组合放过去（llm_intent 里踩过）。

    主键一律不当度量：`id` 的 min/max/avg 是序号统计，写进 prompt 只会诱导模型
    去 SUM 主键。数值列只有 distinct 很小（年份/星级/城市编码这类）才算维度。
    """
    types = {c.get("name"): (c.get("type") or "") for c in cols}
    pks = {c.get("name") for c in cols if c.get("primary_key")}
    rows: List[Dict[str, Any]] = [c for c in (stats.get("columns") or [])
                                  if not c.get("error")]
    fact: Dict[str, Any] = {"table": table, "row_count": stats.get("row_count")}

    for c in rows:
        if _TIME_RE.search(types.get(c["column"], "")) and c.get("min_value") is not None:
            fact["time_range"] = {"column": c["column"],
                                  "start": c["min_value"], "end": c["max_value"]}
            break

    def _numeric(name: str) -> bool:
        return bool(_NUMERIC_RE.search(types.get(name, "")))

    # 时间列不进维度：它是"横轴"而不是分类，列进 prompt 只会让模型多一个
    # WHERE 时间 = '2024-01-01 09:00:00' 这种没用过的选择
    dims = [c for c in rows if c.get("top_values")
            and (c.get("distinct_count") or 0) >= 2
            and not _TIME_RE.search(types.get(c["column"], ""))
            and (not _numeric(c["column"])
                 or c["distinct_count"] <= NUMERIC_AS_DIM_MAX)]
    fact["dimensions"] = [{"column": c["column"], "distinct": c["distinct_count"],
                           "top": c["top_values"][:PROMPT_MAX_TOP_VALUES]}
                          for c in dims[:3]]
    meas = [c for c in rows if _numeric(c["column"]) and c["column"] not in pks
            and c.get("min_value") is not None and not c.get("masked")]
    fact["measures"] = [{"column": c["column"], "min": c["min_value"],
                         "max": c["max_value"], "avg": c.get("avg_value")}
                        for c in meas[:3]]
    fact["null_heavy"] = [{"column": c["column"], "ratio": c.get("null_ratio") or 0}
                          for c in rows if (c.get("null_ratio") or 0) >= 0.3][:3]
    fact["masked"] = [c["column"] for c in rows if c.get("masked")]
    return fact


__all__ = ["MetaTool"]
