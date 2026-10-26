"""数据源系统性描述与优化建议（第一阶段「意图理解」的增强输出）。

用户需求：第一阶段界面要有"智能体对所选数据源的系统性描述与分析，
结合具体业务场景讲述，并包含数据库查询优化方面的建议"。

实现分层（对应确认的方案「规则引擎 + LLM 增强」）：
1. **规则层**（本模块的 collect/profiles）：从适配器取真实元数据——表结构、索引、
   行数估计，跑 sql_advisor 的表结构规则，得到一份"硬事实 + 结构化问题清单"；
   这部分无 LLM 也完整可用，且每次结果一致。
2. **LLM 层**（narrate）：只把硬事实交给模型，让它用业务场景的语言讲出来
   （这个库是干什么的、每张表扮演什么角色、改进建议按什么顺序做）。
   提示词里明确禁止编造数字——数字必须来自规则层。

刻意不做的：不让 LLM 自己判断"缺不缺索引"。索引有无是 information_schema 的事实，
交给模型判断只会得到每次不同的答案。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

from .llm import ModelConfig, chat_json
from .sql_advisor import (Advice, TableMeta, advise_table,
                          BIG_TABLE, MID_TABLE)
from ..utils.logging import get_logger

log = get_logger("basic.core.source_insight")

MAX_PROFILE_TABLES = 12          # 单次剖析的表上限（每张表 3 次元数据查询）

#: 叙述层（LLM 讲成业务场景）的最短超时。实测 5 张表的真实事实要 150 秒才生成得完，
#: 而通用 LLM_TIMEOUT_S 默认 60 秒 —— 于是每一次剖析都必然超时，还要再花 60 秒
#: 试备用模型，用户白等两分钟最后拿到的还是规则摘要。这里给这一层单独设下限，
#: 让它有机会真的跑完；提问链路本身不再被它拖住（见 build_profile 的 with_narration）。
NARRATE_MIN_TIMEOUT_S = 200


@dataclass
class TableProfile:
    table: str
    row_estimate: Optional[int] = None
    columns: List[Dict[str, Any]] = field(default_factory=list)
    indexes: List[Dict[str, Any]] = field(default_factory=list)
    advices: List[Advice] = field(default_factory=list)
    fact: Dict[str, Any] = field(default_factory=dict)   # 内容画像（值/分布/时间跨度）
    role: str = ""               # LLM 给的业务角色一句话

    def as_dict(self) -> Dict[str, Any]:
        return {"table": self.table, "row_estimate": self.row_estimate,
                "columns": self.columns, "indexes": self.indexes,
                "advices": [a.as_dict() for a in self.advices],
                "fact": self.fact, "role": self.role}


def collect(tool: Any, db: str, tables: List[str]) -> List[TableProfile]:
    """取元数据 + 内容画像 + 跑表结构规则。tables 为空则取库内前 N 张表。

    内容画像（fact）是这一步新加的：只有结构事实时，模型能说出"这张表有 12 列"，
    却说不出"里面装的是 2011~2018 年 1.1 万家餐厅、菜系以 North Indian 为主"——
    而后者才是用户要的"对数据源的系统性描述"。
    """
    names = [t for t in (tables or []) if t][:MAX_PROFILE_TABLES]
    if not names:
        try:
            names = tool.list_tables(db)[:MAX_PROFILE_TABLES]
        except Exception as e:  # noqa: BLE001  表清单取不到时上游已报错，这里再兜一层
            log.warning("取表清单失败 %s：%s", db, type(e).__name__)
            return []

    profiles: List[TableProfile] = []
    for name in names:
        try:
            cols_raw = tool.table_schema(db, name).get("columns") or []
        except Exception as e:  # noqa: BLE001  单表失败不拖垮整体
            log.warning("取表结构失败 %s.%s：%s", db, name, type(e).__name__)
            continue
        columns = [{"name": c.get("name"), "type": c.get("type"),
                    "comment": c.get("comment") or "",
                    "primary_key": bool(c.get("primary_key"))} for c in cols_raw]
        # TableMeta.__post_init__ 会把 dict 归一成 ColumnMetaLite，
        # 所以这里直接传 table_schema 的原生输出，不用手工转换一遍
        meta = TableMeta(name=name, columns=columns,
                         indexes=tool.table_indexes(db, name),
                         row_estimate=tool.table_row_estimate(db, name))
        stats, fact = _profile_stats(tool, db, name, cols_raw)
        profiles.append(TableProfile(
            table=name, row_estimate=meta.row_estimate, columns=columns,
            indexes=meta.indexes, advices=advise_table(meta, stats=stats),
            fact=fact))
    # 行数大的排前面：用户第一眼最该看到的是"哪张表最值得优化"
    profiles.sort(key=lambda p: -(p.row_estimate or 0))
    return profiles


def _profile_stats(tool: Any, db: str, name: str, cols_raw: List[Dict[str, Any]]):
    """内容画像与它的原始聚合结果（一次取数，两处用：规则 + prompt 事实）。

    取不到就返回 (None, {})：结构层规则照旧跑，第一步不能因为画像失败而空白。
    """
    from .meta_tools import PROMPT_MAX_COLUMNS, PROMPT_MAX_ROWS
    try:
        est = tool.table_row_estimate(db, name)
        if est is not None and est > PROMPT_MAX_ROWS:
            return None, {"table": name, "row_count": est,
                          "skipped": f"表约 {est:,} 行，超过画像预算，未做列级统计"}
        stats = tool.table_stats(db, name, max_cols=PROMPT_MAX_COLUMNS,
                                 top_limit=5, use_cache=True)
        return stats, tool.table_fact(name, cols_raw[:PROMPT_MAX_COLUMNS], stats)
    except Exception as e:  # noqa: BLE001
        log.warning("数据源画像降级（仅结构）%s.%s：%s", db, name, type(e).__name__)
        return None, {}


# ---------------------------------------------------------------- LLM 叙述层
_PROMPT = """你是数据分析 Agent 的"数据源剖析"模块。下面是一个数据库的真实元数据：
表结构与索引、内容画像（真实聚合出来的行数/时间跨度/取值分布），
以及规则引擎发现的问题清单。请基于**且仅基于**这些事实输出 JSON：

{{
  "overview": "4-6 句话：这个库服务于什么业务场景？里面实际是什么数据（时间范围、规模、主要维度与度量）？读完能知道"能问什么"。",
  "phenomena": ["数据里真实可见的现象，3-6 条，每条必须带一个来自下方事实的数字"],
  "table_roles": {{"表名": "一句话说明这张表在业务里扮演什么角色"}},
  "optimization_plan": [
    {{"priority": 1, "title": "改进项", "why": "为什么要做（结合业务影响）", "how": "具体怎么做"}}
  ],
  "query_tips": ["面向使用者的查询建议，2-4 条"]
}}

硬性规则：
1. 只能使用给定事实里出现的表名、字段名、数字，**禁止编造任何表、字段或数值**；
2. phenomena 每条都要能指回某条事实（例：某列的空值率、某度量的范围、时间跨度），
   事实里没有的现象不要写 —— 宁可少写；
3. 没有内容画像（只有结构事实）时，只讲组织方式，不要假装了解数据内容；
4. optimization_plan 必须从给定问题清单里选，按"影响面 × 实施成本"排序，最多 6 条；
5. 没有发现问题时就明确说"未发现明显的数据组织问题"，不要硬凑；
6. 用中文，简洁、面向业务，不要堆术语；
7. 只输出 JSON。

数据库：{db}
表数量：{table_count}
总行数估计：{total_rows}

【表结构与索引事实】
{facts}

【内容画像事实（真实聚合结果）】
{content}

【规则引擎发现的问题】
{issues}
"""


def _facts_text(profiles: List[TableProfile]) -> str:
    lines: List[str] = []
    for p in profiles:
        cols = ", ".join(
            f"{c['name']}({c['type']}" + (f", {c['comment']}" if c.get("comment") else "")
            + ")" for c in p.columns[:24])
        idx = "；".join(f"{i.get('name')}({', '.join(i.get('columns') or [])})"
                        for i in p.indexes) or "无索引"
        lines.append(f"- 表 {p.table}｜约 {p.row_estimate if p.row_estimate is not None else '未知'} 行"
                     f"｜索引：{idx}\n  字段：{cols}")
    return "\n".join(lines)


def content_text(profiles: List[TableProfile]) -> str:
    """把各表的内容画像渲染成 prompt 文本（格式与 schema_context 共用一处实现）。"""
    from .meta_tools import MetaTool
    facts = [p.fact for p in profiles if p.fact]
    return MetaTool.facts_text(facts) or "（本次未取到内容画像，仅有结构事实）"


def _issues_text(profiles: List[TableProfile]) -> str:
    lines: List[str] = []
    for p in profiles:
        for a in p.advices:
            lines.append(f"- [{a.severity}] {p.table}: {a.title}（依据：{a.evidence}）")
    return "\n".join(lines) if lines else "（未发现问题）"


def narrate(db: str, profiles: List[TableProfile], model: ModelConfig,
            fallback: Optional[ModelConfig] = None) -> Dict[str, Any]:
    """LLM 增强：把硬事实讲成业务语言。失败时抛异常，由调用方回落到规则摘要。"""
    total = sum(p.row_estimate or 0 for p in profiles)
    prompt = _PROMPT.format(
        db=db, table_count=len(profiles), total_rows=f"{total:,}",
        facts=_facts_text(profiles), content=content_text(profiles),
        issues=_issues_text(profiles))
    return chat_json(model,
                     system="你是资深数据架构师兼业务分析师，擅长把数据库元数据与"
                            "数据分布翻译成业务语言。",
                     user=prompt, fallback=fallback)


def fallback_phenomena(profiles: List[TableProfile]) -> List[str]:
    """无 LLM 时的"数据里的现象"：逐条摘自真实聚合结果，不含任何推断。

    这是规则层能给的最强版本 —— 只有结构事实（内容画像失败/超预算）时返回空，
    由调用方决定怎么交代，而不是编几条看起来像洞察的话。
    """
    out: List[str] = []
    for p in profiles:
        f = p.fact
        if not f or f.get("skipped"):
            continue
        tr = f.get("time_range")
        if tr:
            out.append(f"数据覆盖时间范围：{p.table}.{tr['column']} "
                       f"{tr['start']} ~ {tr['end']}。")
        for d in (f.get("dimensions") or [])[:2]:
            if d.get("top"):
                vals = "、".join(f"{v['value']}({v['count']})" for v in d["top"][:5])
                out.append(f"{p.table}.{d['column']} 有 {d['distinct']} 种取值，"
                           f"高频的是：{vals}。")
        for m in (f.get("measures") or [])[:2]:
            rng = f"{m['min']} ~ {m['max']}"
            if m.get("avg") is not None:
                rng += f"，均值 {m['avg']}"
            out.append(f"可聚合度量 {p.table}.{m['column']}：{rng}。")
        for n in (f.get("null_heavy") or [])[:2]:
            out.append(f"{p.table}.{n['column']} 空值率 {n['ratio']:.0%}，"
                       f"按它筛选会得到不完整的结果。")
    return out[:8]


def fallback_overview(db: str, profiles: List[TableProfile]) -> str:
    """无 LLM 时的确定性摘要（同样基于事实，不含推测）。

    内容画像可用时先讲"里面是什么数据"再讲结构问题 —— 只有后者时，
    这份摘要读起来像 DBA 巡检报告，不像在描述一个业务数据集。
    """
    total = sum(p.row_estimate or 0 for p in profiles)
    big = [p for p in profiles if (p.row_estimate or 0) >= BIG_TABLE]
    no_idx = [p for p in profiles if not p.indexes]
    parts = [f"库 {db} 共剖析 {len(profiles)} 张表，总行数估计约 {total:,}。"]

    ph = fallback_phenomena(profiles)
    parts.extend(ph[:3])
    if big:
        parts.append("其中 " + "、".join(p.table for p in big[:3]) + " 属于大表，"
                     "查询时必须带过滤条件与 LIMIT。")
    if no_idx:
        parts.append("、" .join(p.table for p in no_idx[:3]) + " 没有任何索引，"
                     "条件查询会全表扫描。")
    if not ph and not big and not no_idx:
        parts.append("未发现明显的组织方式问题。")
    parts.append("（当前未配置大模型，以上为规则引擎的结构化结论。）")
    return "".join(parts)


def aggregate_advices(profiles: List[TableProfile]) -> List[Advice]:
    """把所有表的问题汇总去重（同一张表同一类问题只留一条）。"""
    seen: set = set()
    out: List[Advice] = []
    for p in profiles:
        for a in p.advices:
            key = (a.code, p.table)
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    out.sort(key=lambda a: (order.get(a.severity, 9), a.code))
    return out


def _narrate_budget(model: ModelConfig,
                    fallback: Optional[ModelConfig]):
    """把叙述层两个模型的超时都抬到下限（备用模型也要：否则主模型一挂又是 60 秒白等）。"""
    def bump(cfg: Optional[ModelConfig]) -> Optional[ModelConfig]:
        if cfg is None or cfg.timeout_s >= NARRATE_MIN_TIMEOUT_S:
            return cfg
        return replace(cfg, timeout_s=NARRATE_MIN_TIMEOUT_S)
    return bump(model), bump(fallback)


def build_profile(*, tool: Any, db: str, tables: List[str],
                  model: ModelConfig,
                  fallback: Optional[ModelConfig] = None,
                  with_narration: bool = True) -> Dict[str, Any]:
    """完整剖析：规则事实 + （可选）LLM 业务化叙述。

    fallback：主模型不可用时可切换的备用模型（见 core.llm.chat 的重试语义）。
    with_narration=False：只出规则层事实与规则摘要（1~2 秒），一次 LLM 都不调。
    这是提问路径的默认取数方式 —— 业务化叙述改由前端在后台单独取（narrate=true），
    否则第一步要对着一个两分钟才失败的转圈。
    （参数不叫 narrate 是因为本模块有个同名函数 narrate()，同名参数会把它遮掉。）
    """
    profiles = collect(tool, db, tables)
    if not profiles:
        return {"database": db, "table_count": 0, "total_row_estimate": 0,
                "tables": [], "advices": [], "overview": "", "phenomena": [],
                "table_roles": {}, "optimization_plan": [], "query_tips": [],
                "model_label": model.label, "llm_used": False,
                "note": "未取到任何表结构，无法剖析"}

    result: Dict[str, Any] = {
        "database": db,
        "table_count": len(profiles),
        "total_row_estimate": sum(p.row_estimate or 0 for p in profiles),
        "tables": [p.as_dict() for p in profiles],
        "advices": [a.as_dict() for a in aggregate_advices(profiles)],
        "model_label": model.label,
        "llm_used": False,
    }

    if with_narration and model.ready:
        model, fallback = _narrate_budget(model, fallback)
        try:
            narrated = narrate(db, profiles, model, fallback=fallback)
            result["overview"] = str(narrated.get("overview") or "")
            ph = narrated.get("phenomena")
            result["phenomena"] = ([str(x).strip() for x in ph if str(x).strip()][:8]
                                   if isinstance(ph, list) else [])
            roles = narrated.get("table_roles") or {}
            result["table_roles"] = roles if isinstance(roles, dict) else {}
            plan = narrated.get("optimization_plan") or []
            result["optimization_plan"] = plan if isinstance(plan, list) else []
            tips = narrated.get("query_tips") or []
            result["query_tips"] = tips if isinstance(tips, list) else []
            result["llm_used"] = True
            for p in profiles:
                p.role = str(result["table_roles"].get(p.table) or "")
            return result
        except Exception as e:  # noqa: BLE001  LLM 失败不该让整个剖析失败
            log.warning("数据源描述 LLM 增强失败，回落规则摘要：%s", type(e).__name__)
            result["note"] = f"大模型解读失败（{type(e).__name__}），已回落为规则结论"

    result["overview"] = fallback_overview(db, profiles)
    result["phenomena"] = fallback_phenomena(profiles)
    result["table_roles"] = {}
    result["optimization_plan"] = []
    result["query_tips"] = []
    return result


__all__ = ["build_profile", "collect", "narrate", "fallback_overview",
           "fallback_phenomena", "aggregate_advices", "TableProfile",
           "MAX_PROFILE_TABLES"]
