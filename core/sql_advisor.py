"""SQL 优化建议引擎（规则层）。

定位：把**可验证的硬事实**变成**可执行的建议**。所有规则都是确定性的 ——
同一条 SQL + 同一份元数据，输出必须完全一致。LLM 只在这之上做业务场景解读
（见 source_insight / agent_api），不参与判断，否则同一个问题每次说法都不一样。

硬事实来自两处：
- SQL 结构：sqlglot AST（与安全门同一个解析器，"能执行的 SQL 一定能解析"）；
- 表元数据：适配器提供的列类型 / 索引 / 行数估计（information_schema 实取，不猜）。

每条建议都带 evidence（依据）—— 没有依据的建议就是猜测，不如不提。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import sqlglot
from sqlglot import exp

# 行数量级阈值：用来决定"全表扫描"这类问题值不值得提。
# InnoDB 的 TABLE_ROWS 是估算值，只用于分档，不当精确数字用。
BIG_TABLE = 100_000
MID_TABLE = 10_000

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


@dataclass
class Advice:
    """一条优化建议。code 供前端做图标/归类，severity 决定排序与配色。"""
    code: str
    severity: str                     # high / medium / low / info
    title: str
    detail: str
    suggestion: str
    evidence: str

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "severity": self.severity, "title": self.title,
                "detail": self.detail, "suggestion": self.suggestion,
                "evidence": self.evidence}


@dataclass
class ColumnMetaLite:
    name: str
    type: str = ""
    primary_key: bool = False


@dataclass
class TableMeta:
    """一张表的优化相关元数据（由 MetaTool 组装后传入）。

    columns 允许直接传 dict（`MetaTool.table_schema()` 的原生输出就是 dict 列表）——
    这里统一归一成 ColumnMetaLite。少了这层归一，最自然的调用姿势会 AttributeError
    （自己写验证脚本时就踩到了）。
    """
    name: str
    columns: List[Any] = field(default_factory=list)
    indexes: List[Dict[str, Any]] = field(default_factory=list)
    row_estimate: Optional[int] = None

    def __post_init__(self) -> None:
        norm: List[ColumnMetaLite] = []
        for c in self.columns or []:
            if isinstance(c, ColumnMetaLite):
                norm.append(c)
            elif isinstance(c, dict):
                norm.append(ColumnMetaLite(name=str(c.get("name") or ""),
                                           type=str(c.get("type") or ""),
                                           primary_key=bool(c.get("primary_key"))))
            else:      # 兜底：别的对象也按属性取，取不到给空值
                norm.append(ColumnMetaLite(name=str(getattr(c, "name", "") or ""),
                                           type=str(getattr(c, "type", "") or ""),
                                           primary_key=bool(getattr(c, "primary_key", False))))
        self.columns = norm

    def indexed_columns(self) -> set:
        out: set = set()
        for idx in self.indexes:
            for c in idx.get("columns") or []:
                if c:
                    out.add(str(c).lower())
        return out

    def indexed_leading_columns(self) -> set:
        """只取每个索引的第一列——复合索引 (a,b) 对单独查 b 是没有用的。"""
        out: set = set()
        for idx in self.indexes:
            cols = idx.get("columns") or []
            if cols and cols[0]:
                out.add(str(cols[0]).lower())
        return out

    def column_type(self, name: str) -> str:
        for c in self.columns:
            if c.name.lower() == (name or "").lower():
                return c.type or ""
        return ""

    def primary_keys(self) -> List[str]:
        return [c.name for c in self.columns if c.primary_key]

    def size_label(self) -> str:
        if self.row_estimate is None:
            return "行数未知"
        if self.row_estimate >= BIG_TABLE:
            return f"约 {self.row_estimate:,} 行（大表）"
        if self.row_estimate >= MID_TABLE:
            return f"约 {self.row_estimate:,} 行（中表）"
        return f"约 {self.row_estimate:,} 行（小表）"


# ---------------------------------------------------------------- 类型判定
_TEXT_TYPES = ("char", "text", "varchar", "string", "clob", "enum", "json")
_NUM_TYPES = ("int", "decimal", "numeric", "float", "double", "real", "bit")
_DATE_TYPES = ("date", "time", "year")
_MONEY_HINT = re.compile(r"(amount|price|cost|value|total|fee|money|金额|价格|总额|费用)", re.I)
_DATE_HINT = re.compile(r"(date|time|day|month|year|日期|时间)", re.I)


def _is_text(t: str) -> bool:
    t = (t or "").lower()
    return any(k in t for k in _TEXT_TYPES)


def _is_number(t: str) -> bool:
    t = (t or "").lower()
    return any(k in t for k in _NUM_TYPES)


def _is_float(t: str) -> bool:
    t = (t or "").lower()
    return "float" in t or "double" in t or "real" in t


def _is_date(t: str) -> bool:
    t = (t or "").lower()
    return any(k in t for k in _DATE_TYPES)


# ---------------------------------------------------------------- SQL 层规则
def advise_sql(sql: str, tables: Dict[str, TableMeta], *,
               max_limit: int = 1000) -> tuple[List[Advice], Optional[str]]:
    """分析一条 SQL，返回 (建议列表, 解析失败原因)。

    解析失败不抛异常：返回空列表 + 原因，由上层决定提示（前端仍可展示原文）。
    """
    raw = (sql or "").strip().rstrip(";").strip()
    if not raw:
        return [], "SQL 为空"
    try:
        trees = sqlglot.parse(raw, read="mysql")
    except Exception as e:  # noqa: BLE001  只回类型，不回片段
        return [], f"SQL 语法解析失败：{type(e).__name__}"
    if len(trees) != 1 or trees[0] is None:
        return [], "只支持分析单条 SELECT"

    tree = trees[0]
    advices: List[Advice] = []
    single = tables.get(next(iter(tables))) if len(tables) == 1 else None

    _rule_select_star(tree, single, advices)
    _rule_missing_limit(tree, max_limit, advices)
    _rule_full_scan(tree, tables, advices)
    _rule_function_on_column(tree, tables, advices)
    _rule_leading_wildcard(tree, advices)
    _rule_order_unbounded(tree, tables, advices)
    _rule_implicit_cast(tree, tables, advices)
    _rule_cross_join(tree, advices)
    _rule_or_to_in(tree, advices)
    _rule_deep_offset(tree, advices)
    _rule_where_without_index(tree, tables, advices)

    advices.sort(key=lambda a: (SEVERITY_ORDER.get(a.severity, 9), a.code))
    return advices, None


def _selects(tree: exp.Expression) -> List[exp.Select]:
    """取所有 Select 节点（含 UNION 的各支）。"""
    return [n for n in tree.walk() if isinstance(n, exp.Select)]


def _table_of(col: exp.Column, tables: Dict[str, TableMeta],
              default: Optional[TableMeta]) -> Optional[TableMeta]:
    tbl = (col.table or "").lower()
    if tbl:
        return tables.get(tbl)
    return default


def _rule_select_star(tree: exp.Expression, single: Optional[TableMeta],
                      out: List[Advice]) -> None:
    if not list(tree.find_all(exp.Star)):
        return
    n_cols = len(single.columns) if single else None
    out.append(Advice(
        code="select_star", severity="medium",
        title="避免 SELECT *，按需取列",
        detail=("SELECT * 会把表里所有列（含大文本/JSON 列）都拉回来，"
                "网络传输与反序列化都是浪费，表结构一变结果列也跟着变。"),
        suggestion="只写查询真正用到的列，并给聚合列起别名。",
        evidence=(f"该表共 {n_cols} 列" if n_cols else "SELECT * 出现在查询中"),
    ))


def _rule_missing_limit(tree: exp.Expression, max_limit: int,
                        out: List[Advice]) -> None:
    if tree.args.get("limit") is not None:
        return
    out.append(Advice(
        code="missing_limit", severity="medium",
        title="缺少 LIMIT 行数封顶",
        detail=("交互式查询没有行数上限时，一次误操作可能把上百万行拉进内存；"
                "后端虽会强制补 LIMIT，但显式写出来更可控、也更利于复用。"),
        suggestion=f"在语句末尾加 LIMIT {max_limit}（或更小的业务上限）。",
        evidence="AST 中不存在 LIMIT 子句",
    ))


def _rule_full_scan(tree: exp.Expression, tables: Dict[str, TableMeta],
                    out: List[Advice]) -> None:
    for sel in _selects(tree):
        if sel.args.get("where") is not None:
            continue
        refs = [t for t in sel.find_all(exp.Table)]
        for ref in refs:
            meta = tables.get((ref.name or "").lower())
            if not meta or meta.row_estimate is None:
                continue
            if meta.row_estimate < MID_TABLE:
                continue
            severity = "high" if meta.row_estimate >= BIG_TABLE else "medium"
            out.append(Advice(
                code="full_scan_no_where", severity=severity,
                title=f"对 {meta.name} 的无条件全表扫描",
                detail=("没有 WHERE 条件时会扫描全表。数据量大时不仅慢，"
                        "还会把缓冲池冲掉，影响同库其他查询。"),
                suggestion="补充时间范围/状态等过滤条件；若确实要全量，请分页拉取。",
                evidence=f"{meta.name} {meta.size_label()}，且该 SELECT 无 WHERE 子句",
            ))


def _rule_function_on_column(tree: exp.Expression, tables: Dict[str, TableMeta],
                             out: List[Advice]) -> None:
    """WHERE 里对列套函数 → 索引失效（B-Tree 存的是原值，不是函数结果）。"""
    default = next(iter(tables.values())) if len(tables) == 1 else None
    for node in tree.walk():
        if not isinstance(node, exp.Func):
            continue
        args = [a for a in node.args.values() if isinstance(a, exp.Column)]
        if not args:
            continue
        col = args[0]
        meta = _table_of(col, tables, default)
        if not meta or col.name.lower() not in meta.indexed_leading_columns():
            continue
        fn = node.sql_name()
        out.append(Advice(
            code="function_on_indexed_column", severity="high",
            title=f"对已索引列 {col.name} 套用了 {fn}()，索引将失效",
            detail=(f"{fn}({col.name}) 会让 MySQL 无法使用 {col.name} 上的索引，"
                    "只能全表扫描后逐行计算。"),
            suggestion=_range_rewrite_hint(fn, col.name, meta),
            evidence=(f"{col.name} 是索引首列；索引："
                      f"{_index_label(meta, col.name)}"),
        ))


def _range_rewrite_hint(fn: str, col: str, meta: TableMeta) -> str:
    """能给出具体改写就给具体改写（日期函数最常见），否则给通用方向。"""
    fn_l = fn.lower()
    if fn_l in ("year", "month", "day") and (_is_date(meta.column_type(col))
                                             or _DATE_HINT.search(col)):
        if fn_l == "year":
            return (f"改成范围条件，例如 {col} >= '2025-01-01' AND {col} < '2026-01-01'"
                    f"（把年份比较换成区间，索引才能用上）。")
        return (f"把 {fn}({col}) 的比较换成 {col} 的日期区间，"
                f"或为表达式建生成列索引。")
    return f"改为对 {col} 本身做范围/等值比较；确需函数计算时可考虑生成列索引。"


def _rule_leading_wildcard(tree: exp.Expression, out: List[Advice]) -> None:
    for node in tree.walk():
        if not isinstance(node, (exp.Like, exp.ILike)):
            continue
        right = node.args.get("expression")
        if isinstance(right, exp.Literal) and right.is_string:
            val = right.this or ""
            if val.startswith("%"):
                out.append(Advice(
                    code="leading_wildcard_like", severity="medium",
                    title="LIKE 前缀通配符用不上索引",
                    detail=("以 % 开头的 LIKE 无法走 B-Tree 索引（前缀不确定），"
                            "只能全表扫描。"),
                    suggestion="改为后缀通配（'abc%'）或前缀精确匹配；"
                               "确需模糊搜索应考虑全文索引。",
                    evidence=f"匹配串以 % 开头：'{val[:24]}'",
                ))
                break


def _rule_order_unbounded(tree: exp.Expression, tables: Dict[str, TableMeta],
                          out: List[Advice]) -> None:
    if tree.args.get("limit") is not None:
        return
    order = tree.args.get("order")
    if order is None:
        return
    default = next(iter(tables.values())) if len(tables) == 1 else None
    for ordered in order.expressions:
        col = ordered.this if isinstance(ordered, exp.Ordered) else ordered
        if not isinstance(col, exp.Column):
            continue
        meta = _table_of(col, tables, default)
        if not meta or meta.row_estimate is None or meta.row_estimate < MID_TABLE:
            continue
        if col.name.lower() in meta.indexed_leading_columns():
            continue
        out.append(Advice(
            code="order_by_unbounded", severity="medium",
            title=f"大表 ORDER BY {col.name} 且无 LIMIT",
            detail=("排序列没有索引支撑时会产生 filesort（临时表排序），"
                    "数据量大时既吃 CPU 也吃磁盘。"),
            suggestion=f"为 {meta.name}({col.name}) 建索引，或给排序加上 LIMIT 缩小排序量。",
            evidence=(f"{meta.name} {meta.size_label()}；{col.name} 不在任何索引首列上"),
        ))
        break


def _rule_implicit_cast(tree: exp.Expression, tables: Dict[str, TableMeta],
                        out: List[Advice]) -> None:
    """字符串列与数字字面量比较 → 隐式类型转换，索引失效。"""
    default = next(iter(tables.values())) if len(tables) == 1 else None
    seen: set = set()
    for node in tree.walk():
        if not isinstance(node, (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ)):
            continue
        left, right = node.args.get("this"), node.args.get("expression")
        col, literal = (left, right) if isinstance(left, exp.Column) else \
                       (right, left) if isinstance(right, exp.Column) else (None, None)
        if col is None or not isinstance(literal, exp.Literal) or not literal.is_number:
            continue
        meta = _table_of(col, tables, default)
        if not meta or not _is_text(meta.column_type(col.name)):
            continue
        key = (col.name.lower(), literal.this)
        if key in seen:
            continue
        seen.add(key)
        out.append(Advice(
            code="implicit_cast", severity="medium",
            title=f"{col.name} 是文本类型，却与数字 {literal.this} 比较",
            detail=(f"{col.name} 的实际类型是 {meta.column_type(col.name)}，"
                    "与数字比较会触发隐式转换，该列上的索引直接失效。"),
            suggestion=f"比较时加引号：{col.name} = '{literal.this}'，"
                       "或把列类型改为数值型。",
            evidence=f"列类型 {meta.column_type(col.name)}；比较值 {literal.this} 为数字字面量",
        ))


def _rule_cross_join(tree: exp.Expression, out: List[Advice]) -> None:
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is None and not join.args.get("using"):
            # 逗号连接（FROM a, b）在 sqlglot 里也是 Join，但没有 on
            out.append(Advice(
                code="cross_join", severity="high",
                title="存在没有连接条件的 JOIN（笛卡尔积）",
                detail="两张表没有任何关联条件时会做笛卡尔积，结果行数是两表行数相乘。",
                suggestion="补充 ON 条件，或确认是否真的需要交叉组合。",
                evidence="JOIN 节点缺少 ON/USING 子句",
            ))
            break


def _rule_or_to_in(tree: exp.Expression, out: List[Advice]) -> None:
    for node in tree.find_all(exp.Or):
        cols: Dict[str, List[str]] = {}
        for sub in node.walk():
            if isinstance(sub, exp.EQ) and isinstance(sub.this, exp.Column) \
                    and isinstance(sub.expression, exp.Literal):
                cols.setdefault(sub.this.name.lower(), []).append(
                    sub.expression.this or "")
        for col, vals in cols.items():
            if len(vals) >= 3:
                out.append(Advice(
                    code="or_to_in", severity="low",
                    title=f"同一列 {col} 用了 {len(vals)} 个 OR 相等条件",
                    detail="多个 OR 相等可读性差，优化器也更容易选错索引。",
                    suggestion=f"改为 {col} IN ({', '.join(repr(v) for v in vals[:6])}"
                               + (" …" if len(vals) > 6 else "") + ")。",
                    evidence=f"检测到 {len(vals)} 个针对 {col} 的 OR 条件",
                ))
                break
        break


def _rule_deep_offset(tree: exp.Expression, out: List[Advice]) -> None:
    offset = tree.args.get("offset")
    if offset is None:
        return
    val = ""
    try:
        val = offset.expression.name
    except Exception:  # noqa: BLE001  表达式型 OFFSET 跳过
        return
    if val.isdigit() and int(val) >= 10_000:
        out.append(Advice(
            code="deep_offset", severity="low",
            title=f"OFFSET {val} 过大，深分页成本高",
            detail=("大 OFFSET 需要先扫描并丢弃前面所有行，页数越深越慢。"),
            suggestion="改用 keyset 分页：WHERE (排序键) > 上一页最后一个值 ORDER BY 排序键 LIMIT n。",
            evidence=f"OFFSET = {val}",
        ))


def _rule_where_without_index(tree: exp.Expression, tables: Dict[str, TableMeta],
                              out: List[Advice]) -> None:
    """WHERE 的等值/范围列完全没有索引（表有相当行数时才值得提）。"""
    default = next(iter(tables.values())) if len(tables) == 1 else None
    seen: set = set()
    for node in tree.walk():
        if not isinstance(node, (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            continue
        col = node.this if isinstance(node.this, exp.Column) else \
              node.expression if isinstance(node.expression, exp.Column) else None
        if col is None:
            continue
        meta = _table_of(col, tables, default)
        if not meta or meta.row_estimate is None or meta.row_estimate < MID_TABLE:
            continue
        if col.name.lower() in meta.indexed_leading_columns():
            continue
        if col.name.lower() in seen:
            continue
        seen.add(col.name.lower())
        severity = "high" if meta.row_estimate >= BIG_TABLE else "medium"
        out.append(Advice(
            code="where_column_not_indexed", severity=severity,
            title=f"过滤列 {col.name} 上没有索引",
            detail=(f"在 {meta.size_label()} 上按 {col.name} 过滤，"
                    "没有索引只能全表扫描。"),
            suggestion=f"考虑为 {meta.name}({col.name}) 建索引；"
                       "写多列条件时优先建复合索引并注意最左前缀。",
            evidence=(f"{meta.name} {meta.size_label()}；现有索引："
                      f"{_indexes_label(meta)}"),
        ))


# ---------------------------------------------------------------- 表结构层规则
def advise_table(meta: TableMeta, stats: Optional[Dict[str, Any]] = None) -> List[Advice]:
    """单张表的"数据组织方式"建议（索引/主键/类型选型）。"""
    out: List[Advice] = []
    rows = meta.row_estimate

    if not meta.primary_keys():
        out.append(Advice(
            code="no_primary_key", severity="high",
            title=f"{meta.name} 没有主键",
            detail=("无主键表无法行级定位，更新/删除容易误伤全表，"
                    "主从复制与备份恢复也更慢。"),
            suggestion="补充自增 ID 或业务唯一键作为主键。",
            evidence="information_schema 中该表无 PRIMARY KEY",
        ))

    if not meta.indexes and rows is not None and rows >= 1000:
        out.append(Advice(
            code="table_no_index", severity="medium",
            title=f"{meta.name} 有数据但没有任何二级索引",
            detail="任何按条件查询都只能全表扫描，随数据增长会线性变慢。",
            suggestion="按常用查询条件补索引（通常先给时间列/状态列建）。",
            evidence=f"{meta.size_label()}；索引数量 0",
        ))

    for c in meta.columns:
        t = c.type or ""
        if _is_float(t) and _MONEY_HINT.search(c.name):
            out.append(Advice(
                code="float_for_money", severity="medium",
                title=f"金额类字段 {c.name} 用了浮点类型 {t}",
                detail=("浮点二进制无法精确表示十进制小数，累加与对账会出现 "
                        "0.01 级误差，且误差会随行数放大。"),
                suggestion=f"改为 DECIMAL(M,2)（金额）或 DECIMAL(M,4)（单价/比率）。",
                evidence=f"列类型 {t}；列名命中金额语义",
            ))
        if _is_text(t) and not c.primary_key and _DATE_HINT.search(c.name):
            out.append(Advice(
                code="text_date_column", severity="low",
                title=f"时间字段 {c.name} 用文本类型 {t} 存储",
                detail=("文本日期无法直接比较大小，区间查询与索引都受影响，"
                        "还容易出现 '2025-1-5' 与 '2025-01-05' 两种写法。"),
                suggestion="改为 DATE / DATETIME；历史数据可批量转换后切换。",
                evidence=f"列类型 {t}；列名命中时间语义",
            ))

    if stats:
        for col in stats.get("columns") or []:
            ratio = col.get("null_ratio") or 0
            if ratio >= 0.5 and (col.get("distinct_count") or 0) <= 5:
                out.append(Advice(
                    code="high_null_low_cardinality", severity="low",
                    title=f"{col.get('column')} 空值率 {ratio:.0%} 且取值很少",
                    detail="高空值低基数的列对分析价值有限，还会拖慢聚合。",
                    suggestion="确认是否仍需要该列；可考虑默认值或拆分为独立标志位。",
                    evidence=f"空值率 {ratio:.0%}；distinct {col.get('distinct_count')}",
                ))
    return out


# ---------------------------------------------------------------- 辅助
def _indexes_label(meta: TableMeta) -> str:
    if not meta.indexes:
        return "无"
    parts = []
    for idx in meta.indexes:
        cols = ", ".join(idx.get("columns") or [])
        parts.append(f"{idx.get('name')}({cols})")
    return "；".join(parts[:4])


def _index_label(meta: TableMeta, column: str) -> str:
    for idx in meta.indexes:
        if (idx.get("columns") or [None])[0] and \
                str(idx["columns"][0]).lower() == (column or "").lower():
            return f"{idx.get('name')}({', '.join(idx.get('columns') or [])})"
    return "—"


__all__ = ["Advice", "TableMeta", "ColumnMetaLite", "advise_sql", "advise_table",
           "BIG_TABLE", "MID_TABLE"]
