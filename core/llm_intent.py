"""LLM 意图理解模块（设计文档 §9.3）。

可插拔设计：统一 LLMClient 接口——
- OpenAICompatClient：任何 OpenAI 兼容端点（通义千问 / DeepSeek / GLM / Moonshot），
  模型配置由 core.llm.resolve_model 解析（前端「模型管理」里激活的模型优先）；
- RuleBasedClient：离线规则兜底（无 key 也能跑通全链路，供演示与测试）。

HTTP 细节（超时/重试/JSON 容错）统一在 core.llm，本模块只负责提示词与字段映射。

输出统一为 IntentConfirmCard 字段：intent / target_db / target_tables /
target_fields / sql_preview / risk_level / hint。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from ..config.settings import Settings
from ..models.pydantic_schemas import IntentConfirmCard
from ..utils.logging import get_logger
from .llm import ModelConfig, chat_json

log = get_logger("basic.core.llm_intent")

# 意图关键词（规则兜底用；中文问法优先）
_INTENT_RULES: List[tuple[str, str]] = [
    ("时间趋势分析", ["趋势", "走势", "每月", "按月", "同期", "环比", "同比", "增长", "变化"]),
    ("分类对比分析", ["对比", "比较", "各", "每个", "分别", "排名", "top", "哪些", "哪个"]),
    ("占比分析", ["占比", "比例", "份额", "构成", "分布"]),
    ("汇总统计", ["多少", "总共", "一共", "总计", "总和", "总量", "总数"]),
]

_PROMPT = """你是数据分析Agent的意图解析器。根据用户问题和数据库Schema，输出JSON：
{{
  "intent": "意图类型（时间趋势分析/分类对比分析/占比分析/汇总统计/数据查询）",
  "target_db": "目标数据库名",
  "target_tables": ["目标表名"],
  "target_fields": ["目标字段名"],
  "sql_preview": "拟生成的只读SQL（单条SELECT，必须LIMIT）",
  "risk_level": "low 或 medium",
  "hint": "一句话说明你的理解"
}}
硬性规则：
1. 只能使用Schema中给出的表与字段；
2. SQL必须是单条SELECT；
3. 时间筛选用显式日期范围；
4. {ref_rule}
用户问题：{question}
Schema：
{schema}
只输出JSON对象，不要解释。"""

# 表引用写法随数据源类型变化，提示词必须跟着变，否则生成出来的 SQL 跑不通
_REF_RULE_QUALIFIED = ("表名一律写成 `库名`.`表名` 的完整形式（例如 `shop`.`t_order`）。"
                       "这一点很重要：连接默认的库未必是 target_db，写裸表名会查到错的库。")
_REF_RULE_BARE = ("SQL 里直接写表名（`表名`），不要加库前缀——这是单库数据源，"
                  "加库前缀会被当成不存在的表。")


def _build_prompt(question: str, schema: str, qualifies: bool = True) -> str:
    return _PROMPT.format(question=question, schema=schema,
                          ref_rule=_REF_RULE_QUALIFIED if qualifies else _REF_RULE_BARE)


# ---------------------------------------------------------------- 客户端接口
class LLMClient:
    name = "base"

    def parse_intent(self, question: str, schema_context: str) -> Dict[str, Any]:
        raise NotImplementedError


class OpenAICompatClient(LLMClient):
    """OpenAI 兼容 chat/completions 客户端（HTTP 细节委托 core.llm）。"""

    name = "openai-compatible"

    def __init__(self, settings: Settings, qualifies: bool = True,
                 model: Optional[ModelConfig] = None,
                 fallback: Optional[ModelConfig] = None) -> None:
        self.settings = settings
        self.qualifies = qualifies
        self.model = model or ModelConfig(
            name=settings.llm_model,
            api_base=(settings.llm_api_base or "").rstrip("/"),
            api_key=settings.llm_api_key or "",
            model=settings.llm_model or "",
            timeout_s=settings.llm_timeout_s,
        )
        self.fallback = fallback

    def parse_intent(self, question: str, schema_context: str) -> Dict[str, Any]:
        return chat_json(self.model,
                         system="你是严谨的数据分析SQL助手，只输出JSON。",
                         user=_build_prompt(question, schema_context, self.qualifies),
                         fallback=self.fallback)


class RuleBasedClient(LLMClient):
    """规则兜底：关键词意图识别 + Schema 名词匹配 + 模板化 SQL 预览。

    覆盖不了的长尾问句会给出保守意图与提示，由用户"修改问题"重试。
    """

    name = "rule-based"

    def __init__(self, schema_context: str, selected_tables: List[str],
                 selected_fields: List[str], default_db: str,
                 qualify: "Callable[[str, str], str] | None" = None) -> None:
        # 从 schema 文本解析 表→字段 映射（格式见 MetaTool.schema_context）
        self.tables: Dict[str, List[str]] = _parse_schema_text(schema_context)
        # 同时留下字段类型：判断"哪一列能求和"必须看类型，不能靠列名猜
        self.types: Dict[str, Dict[str, str]] = _parse_schema_types(schema_context)
        self.selected_tables = selected_tables or list(self.tables)[:1]
        self.selected_fields = selected_fields
        self.default_db = default_db
        # 表引用怎么拼由数据源决定（MySQL `db`.`t`，SQLite 只 `t`）
        self.qualify = qualify or _default_qualify

    def _numeric_cols(self, table: str, cols: List[str]) -> List[str]:
        """能参与 SUM/AVG 的列：先看 Schema 类型，类型拿不到再退回列名启发式。"""
        types = self.types.get(table, {})
        by_type = [c for c in cols if _type_is_numeric(types.get(c, ""))]
        if by_type:
            return by_type
        return [c for c in cols if _looks_numeric(c)]

    def parse_intent(self, question: str, schema_context: str) -> Dict[str, Any]:
        q = question.strip()
        intent = "数据查询"
        for name, kws in _INTENT_RULES:
            if any(k.lower() in q.lower() for k in kws):
                intent = name
                break

        # 目标表：问句命中表名 > 用户勾选 > 第一张表
        tables = [t for t in self.tables if t in q] or list(self.selected_tables)
        if not tables:
            tables = list(self.tables)[:1]
        table = tables[0]
        cols = self.tables.get(table, [])

        # 目标字段：问句命中列名 > 勾选字段 > 兜底（首个数值列或首列）
        fields = [c for c in cols if c in q]
        if not fields:
            fields = [c for c in self.selected_fields if c in cols]
        if not fields:
            fields = self._numeric_cols(table, cols)[:1] or cols[:1]

        # 度量列：问句命中的数值列 > 表里第一个数值列 > 命中字段的第一个
        num_cols = self._numeric_cols(table, cols)
        val = next((c for c in fields if c in num_cols), None) \
            or (num_cols[0] if num_cols else (fields[0] if fields else cols[0]))
        # 字段列表把度量列排前面，避免下游再猜一次
        fields = _ordered_unique([val] + [c for c in fields if c != val])

        sql_preview, risk = _template_sql(intent, self.default_db, table, cols, fields, q,
                                          val, self.qualify)
        hint = f"规则兜底识别（{intent}）；目标表 {table}。可点「修改问题」补充条件后重试。"
        return {"intent": intent, "target_db": self.default_db,
                "target_tables": tables[:3], "target_fields": fields[:5],
                "sql_preview": sql_preview, "risk_level": risk, "hint": hint}


# ---------------------------------------------------------------- 入口
def parse_intent(*, question: str, schema_context: str, selected_db: str,
                 selected_tables: List[str], selected_fields: List[str],
                 settings: Settings,
                 qualify: "Callable[[str, str], str] | None" = None,
                 qualifies: bool = True,
                 model: Optional[ModelConfig] = None,
                 fallback: Optional[ModelConfig] = None) -> tuple[IntentConfirmCard, str, str]:
    """解析意图，返回 (IntentConfirmCard, 实际使用的客户端名, 模型标签)。

    qualify   —— 把 (库, 表) 拼成数据源能识别的引用，规则兜底生成模板 SQL 时用；
                 不传则按 MySQL 风格 `db`.`table`（对 SQLite 文件源会拼出非法引用）。
    qualifies —— 提示词里要求 LLM 写库限定名还是裸表名，同上随数据源类型变化。
    model     —— 由 core.llm.resolve_model 解析出的模型配置；不传则按环境变量构造。
    fallback  —— 主模型不可用（可重试类错误）时自动切换的备用模型，见 core.llm.chat。
    """
    client: LLMClient
    if model is None or model.ready:
        try:
            llm = OpenAICompatClient(settings, qualifies=qualifies, model=model,
                                     fallback=fallback)
            if llm.model.ready:
                card_dict = llm.parse_intent(question, schema_context)
                card = IntentConfirmCard(
                    original_question=question,
                    intent=str(card_dict.get("intent", "数据查询")),
                    target_db=str(card_dict.get("target_db") or selected_db),
                    target_tables=[str(t) for t in card_dict.get("target_tables", [])][:5],
                    target_fields=[str(f) for f in card_dict.get("target_fields", [])][:8],
                    sql_preview=str(card_dict.get("sql_preview", "")),
                    risk_level=card_dict.get("risk_level", "low")
                    if card_dict.get("risk_level") in ("low", "medium") else "low",
                    hint=str(card_dict.get("hint", "")),
                )
                return card, OpenAICompatClient.name, llm.model.label
        except Exception as e:  # noqa: BLE001  LLM 失败降级规则兜底（链路可用性优先）
            # 降级本身是设计内的，但必须留痕：否则用户只看到一条模板 SQL，
            # 以为"AI 就是这个水平"，实际上 LLM 根本没调通。
            log.warning("LLM 意图解析失败，降级为规则兜底：%s: %s",
                        type(e).__name__, e)
    rule = RuleBasedClient(schema_context, selected_tables, selected_fields, selected_db,
                           qualify=qualify)
    d = rule.parse_intent(question, schema_context)
    return (IntentConfirmCard(original_question=question, **d),
            RuleBasedClient.name, "规则兜底")


# ---------------------------------------------------------------- 内部助手
def _parse_schema_text(schema_context: str) -> Dict[str, List[str]]:
    """解析 MetaTool.schema_context 生成的文本：'## 表 t' 之后的 '  - col: TYPE'。"""
    tables: Dict[str, List[str]] = {}
    current = None
    for line in schema_context.splitlines():
        line = line.rstrip()
        if line.startswith("## 表 "):
            current = line[len("## 表 "):].strip()
            tables[current] = []
        elif current and line.strip().startswith("- "):
            col = line.strip()[2:].split(":", 1)[0].strip()
            if col:
                tables[current].append(col)
    return tables


def _parse_schema_types(schema_context: str) -> Dict[str, Dict[str, str]]:
    """同上，但保留字段类型：'  - col: TYPE (PK)' → {表: {列: 类型}}。

    用来判断"哪一列能 SUM"。只按列名猜（金额/数量…）在中文列名上经常落空 ——
    「订单额」既不含"金额"也不含"数量"，就会被当成维度列，生成出
    SUM(平台) 这种荒谬的 SQL。
    """
    types: Dict[str, Dict[str, str]] = {}
    current = None
    for line in schema_context.splitlines():
        line = line.rstrip()
        if line.startswith("## 表 "):
            current = line[len("## 表 "):].strip()
            types[current] = {}
        elif current and line.strip().startswith("- "):
            body = line.strip()[2:]
            if ":" not in body:
                continue
            col, _, rest = body.partition(":")
            col = col.strip()
            if col:
                # " # 注释" 之后是列注释（给模型看的业务含义），不能混进类型里：
                # 注释里出现 date/time/int 会让下面的类型判断误判这一列。
                rest = rest.split(" #", 1)[0]
                types[current][col] = rest.replace("(PK)", "").strip()
    return types


def _type_is_numeric(type_name: str) -> bool:
    """MySQL/SQLite 的数值类型判定。"""
    t = (type_name or "").lower()
    return any(k in t for k in ("int", "decimal", "numeric", "float", "double",
                                "real", "number", "dec"))


def _default_qualify(db: str, table: str) -> str:
    """MySQL 风格的库限定引用（拿不到适配器时的兜底）。"""
    return f"`{db}`.`{table}`"


def _template_sql(intent: str, db: str, table: str, cols: List[str],
                  fields: List[str], question: str, val: str,
                  qualify: "Callable[[str, str], str]" = _default_qualify) -> tuple[str, str]:
    """按意图模板生成预览 SQL（规则兜底专用；最终 SQL 仍走 sql_generator 校验）。

    val（度量列）由调用方按 Schema 类型选好后传进来 —— 模板不该再猜一次。
    """
    ref = qualify(db, table)
    time_col = next((c for c in cols if any(k in c.lower() for k in
                                            ("time", "date", "日期", "时间"))), None)
    year = re.search(r"(20\d{2})", question)
    year_filter = f" WHERE YEAR({time_col}) = {year.group(1)}" if (year and time_col) else ""

    if intent == "时间趋势分析" and time_col:
        sql = (f"SELECT DATE_FORMAT({time_col}, '%Y-%m') AS month, "
               f"SUM({val}) AS total FROM {ref}{year_filter} "
               f"GROUP BY month ORDER BY month")
        return sql, "low"
    if intent == "分类对比分析":
        cat = next((c for c in fields if c != val), None) or \
              next((c for c in cols if c not in (time_col, val)), cols[0])
        sql = (f"SELECT {cat}, SUM({val}) AS total FROM {ref}{year_filter} "
               f"GROUP BY {cat} ORDER BY total DESC")
        return sql, "low"
    if intent == "占比分析":
        cat = next((c for c in fields if c != val), None) or \
              next((c for c in cols if c not in (time_col, val)), cols[0])
        sql = (f"SELECT {cat}, SUM({val}) AS total FROM {ref}{year_filter} "
               f"GROUP BY {cat} ORDER BY total DESC")
        return sql, "low"
    sql = f"SELECT COUNT(*) AS row_count FROM {ref}{year_filter}"
    return sql, "low"


def _looks_numeric(col: str) -> bool:
    """列名启发式：只在拿不到 Schema 类型时兜底（中文列名常落空，别指望它）。"""
    return any(k in col.lower() for k in ("amount", "price", "qty", "count", "num",
                                          "value", "score", "total", "金额", "数量"))


def _ordered_unique(seq: List[str]) -> List[str]:
    """去重且保持顺序（字段列表要给下游一个稳定的顺序）。"""
    return list(dict.fromkeys(seq))


__all__ = ["parse_intent", "LLMClient", "OpenAICompatClient", "RuleBasedClient"]
