"""全部 Pydantic 请求/响应数据模型（设计文档 §6）。

约定：
- 出入参模型与内部状态模型都放这里，接口层只做组装，不重新定义结构；
- Datasource 响应永不回显密码明文，只给 password_set 标记；
- 时间字段统一 ISO 字符串。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 6.1 数据源
class Datasource(BaseModel):
    id: int
    name: str
    type: Literal["mysql", "file"] = "mysql"
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None
    is_active: bool = False
    password_set: bool = False          # 响应用：是否已保存密码（不回显明文）
    created_at: str = ""


class DatasourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: Literal["mysql", "file"] = "mysql"
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = ""
    password: str = ""
    database: str = ""


class DatasourceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None      # 传空串表示保持原密码
    database: Optional[str] = None


# ---------------------------------------------------------------- 6.2 会话状态
class AgentSession(BaseModel):
    session_id: str
    ds_id: int = 0
    user_question: str = ""
    intent_confirmed: bool = False
    sql: Optional[str] = None
    sql_confirmed: bool = False
    query_result: Optional[Dict[str, Any]] = None
    analysis_result: Optional[Dict[str, Any]] = None
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------- 6.3 意图确认卡片
class IntentConfirmCard(BaseModel):
    original_question: str
    intent: str
    target_db: str
    target_tables: List[str] = Field(default_factory=list)
    target_fields: List[str] = Field(default_factory=list)
    sql_preview: str = ""
    risk_level: Literal["low", "medium"] = "low"
    hint: str = ""
    session_id: str = ""                # 接入层补充：链路追踪


# ---------------------------------------------------------------- 6.4 SQL 校验结果
class SqlSecurityResult(BaseModel):
    passed: bool                       # 注意：字段名 pass 与 Python 关键字冲突，用 passed
    reason: Optional[str] = None
    final_sql: Optional[str] = None


# ---------------------------------------------------------------- 6.5 查询结果
class QueryResult(BaseModel):
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    cost_ms: int
    truncated: bool = False            # 触达行数上限被截断的提示


# ---------------------------------------------------------------- 6.6 分析输出
class Insight(BaseModel):
    type: Literal["anomaly", "trend", "comparison"]
    title: str
    detail: str
    evidence: str


class AnalysisResult(BaseModel):
    summary_text: str
    stats: Dict[str, Any] = Field(default_factory=dict)
    insights: List[Insight] = Field(default_factory=list)
    suggest_chart: Literal["table", "bar", "line", "pie"] = "table"
    chart_option: Dict[str, Any] = Field(default_factory=dict)   # 接入层补充：ECharts option


# ---------------------------------------------------------------- API 请求体
class ParseIntentRequest(BaseModel):
    ds_id: Optional[int] = None                     # 缺省取当前激活数据源
    user_query: str = Field(min_length=1)
    selected_db: str = ""
    selected_tables: List[str] = Field(default_factory=list)
    selected_fields: List[str] = Field(default_factory=list)
    session_id: str = ""                            # 有则绑定到这条（须为未提问的空会话）
    project_id: Optional[int] = None                # 新建会话时归属的项目（缺省=未分组）


class GenerateSqlRequest(BaseModel):
    session_id: str = ""
    intent_context: Dict[str, Any]                  # IntentConfirmCard 完整 JSON


class ConfirmSqlRequest(BaseModel):
    session_id: str


class ExecuteSqlRequest(BaseModel):
    session_id: str = ""
    ds_id: Optional[int] = None
    sql: str = ""                                   # 缺省用会话内已确认的 SQL


class AnalysisRequest(BaseModel):
    query_result: Dict[str, Any]                    # QueryResult 完整 JSON
    session_id: str = ""
    intent: str = ""                                # 透传意图，辅助图表推荐


# ---------------------------------------------------------------- 6.8 优化建议
class Advice(BaseModel):
    """一条优化建议（规则引擎产出，evidence 是硬事实依据）。"""
    code: str
    severity: Literal["high", "medium", "low", "info"] = "info"
    title: str
    detail: str = ""
    suggestion: str = ""
    evidence: str = ""


class SqlAdviceRequest(BaseModel):
    sql: str = Field(min_length=1)
    ds_id: Optional[int] = None
    session_id: str = ""
    narrate: bool = False                # 是否调 LLM 做业务解读（按需，避免每次编辑都花 token）


class TableFact(BaseModel):
    """剖析时拿到的表级事实（行数 + 索引），随建议一起回给前端展示。"""
    table: str
    row_estimate: Optional[int] = None
    indexes: List[Dict[str, Any]] = Field(default_factory=list)
    column_count: int = 0


class SqlAdviceResult(BaseModel):
    sql: str
    readable: bool = True                # 是否只读（没过安全门时给提示，不阻断展示）
    parse_error: Optional[str] = None
    advices: List[Advice] = Field(default_factory=list)
    facts: List[TableFact] = Field(default_factory=list)
    summary: str = ""                    # LLM 业务解读；无 LLM 时为规则汇总
    model_label: str = ""
    llm_used: bool = False


class SourceInsightRequest(BaseModel):
    ds_id: Optional[int] = None
    db: str = ""
    tables: List[str] = Field(default_factory=list)   # 空 = 库内前 N 张表
    refresh: bool = False               # 强制跳过缓存重新剖析
    #: 是否要业务化叙述（调 LLM）。默认 false：规则层 1~2 秒就能出事实，
    #: 叙述层单独取（实测要 150 秒以上，不能让第一步干等它）。
    narrate: bool = False


class SourceTableInsight(BaseModel):
    table: str
    row_estimate: Optional[int] = None
    columns: List[Dict[str, Any]] = Field(default_factory=list)
    indexes: List[Dict[str, Any]] = Field(default_factory=list)
    advices: List[Advice] = Field(default_factory=list)
    role: str = ""                       # LLM 给的业务角色一句话


class SourceInsightResult(BaseModel):
    database: str
    table_count: int = 0
    total_row_estimate: int = 0
    overview: str = ""
    # 数据里真实可见的现象（每条都指向内容画像里的一个数字）；无内容画像时为空
    phenomena: List[str] = Field(default_factory=list)
    tables: List[SourceTableInsight] = Field(default_factory=list)
    advices: List[Advice] = Field(default_factory=list)
    table_roles: Dict[str, str] = Field(default_factory=dict)
    optimization_plan: List[Dict[str, Any]] = Field(default_factory=list)
    query_tips: List[str] = Field(default_factory=list)
    model_label: str = ""
    llm_used: bool = False
    from_cache: bool = False
    note: str = ""


class NewSessionRequest(BaseModel):
    ds_id: Optional[int] = None          # 缺省取当前激活数据源
    project_id: Optional[int] = None     # 缺省归入「未分组」


# ---------------------------------------------------------------- 元数据响应
class ColumnMeta(BaseModel):
    name: str
    type: str
    comment: str = ""
    primary_key: bool = False
    nullable: bool = True


class TableSchema(BaseModel):
    database: str
    table: str
    columns: List[ColumnMeta]
    row_estimate: Optional[int] = None


class ColumnStats(BaseModel):
    column: str
    null_count: int = 0
    null_ratio: float = 0.0
    distinct_count: Optional[int] = None
    min_value: Any = None
    max_value: Any = None
    avg_value: Any = None                                                 # 仅数值列
    masked: bool = False                                                  # 敏感列：只给统计
    top_values: List[Dict[str, Any]] = Field(default_factory=list)   # 低基数列频次


class TableStats(BaseModel):
    database: str
    table: str
    row_count: int = 0
    columns: List[ColumnStats] = Field(default_factory=list)


# ---------------------------------------------------------------- 6.7 模型供应商
class ProviderModelItem(BaseModel):
    """供应商下的一个模型。is_active = 当前 Agent 正在用的那个。"""
    id: int
    model_name: str
    is_default: bool = False
    is_active: bool = False


class Provider(BaseModel):
    """供应商（响应）。api_key 永不回显，只给 has_key 标记。"""
    id: int
    provider_key: str
    name: str
    api_base: str
    protocol: str = "/chat/completions"
    enabled: bool = True
    has_key: bool = False
    models: List[ProviderModelItem] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class ProviderCreate(BaseModel):
    provider_key: str = Field(min_length=1, max_length=64,
                              pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=128)
    api_base: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=255)
    protocol: str = "/chat/completions"
    enabled: bool = True
    #: 顺带登记的模型名（第一个会成为该供应商的默认模型）
    models: List[str] = Field(default_factory=list)


class ProviderUpdate(BaseModel):
    provider_key: Optional[str] = Field(default=None, min_length=1, max_length=64,
                                        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    api_base: Optional[str] = Field(default=None, max_length=255)
    api_key: Optional[str] = None     # 传空串表示保持原密钥
    protocol: Optional[str] = None
    enabled: Optional[bool] = None


class ProviderTestRequest(BaseModel):
    """测试连接：表单未保存时用草稿配置直接测。

    不传 model 只验「端点可达 + 密钥有效」；传了才验证该模型是否真的可调用。
    """
    api_base: str = Field(min_length=1, max_length=255)
    api_key: str = ""
    protocol: str = "/chat/completions"
    model: str = ""
    provider_id: Optional[int] = None   # 给了且 api_key 为空 → 复用已存的密钥


class AddModelRequest(BaseModel):
    model_name: str = Field(min_length=1, max_length=128)


class RemoteModelsResponse(BaseModel):
    """GET /models 拉回的供应商侧模型清单。

    supported=False 表示该端点不提供模型列表（404/无 data 字段），
    不代表连接有问题——前端据此提示"手动填写模型名"。
    """
    supported: bool
    models: list[str] = []
    msg: str = ""


class AddModelsBatchRequest(BaseModel):
    """从拉取的清单里勾选登记（列表里有 ≠ 有调用权限，登记后仍可逐个测试）。"""
    model_names: list[str] = Field(default_factory=list, max_length=200)


# ---------------------------------------------------------------- 6.9 项目
class Project(BaseModel):
    id: int
    name: str
    folder_path: str = ""
    session_count: int = 0
    is_default: bool = False
    created_at: str = ""
    updated_at: str = ""


class ProjectCreate(BaseModel):
    name: str = ""                    # 留空则取文件夹名
    folder_path: str = ""             # 留空则在 PROJECTS_DIR 下按名字建


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    folder_path: Optional[str] = None


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class MoveSessionRequest(BaseModel):
    project_id: int


# ---------------------------------------------------------------- 7. 任务清单
#: running 只由批量执行过程自己写入，人（和前端）只在其余三者间切换
TaskStatus = Literal["todo", "running", "done", "failed"]
#: 任务从哪来：手工新建 / 数据源剖析建议 / SQL 优化建议
TaskSourceKind = Literal["manual", "advice", "sql_advice"]


class Task(BaseModel):
    """一条待办（响应）。

    source_ref 是"加入清单那一刻"的证据快照（哪张表、哪条规则、原文数字）：
    建议会随数据变化重算，快照才能让任务三个月后仍然可解释。
    sql 为空表示这条只能人工办结（批量执行时明确跳过，不假装跑了）。
    """
    id: int
    project_id: int
    ds_id: int = 0
    title: str
    note: str = ""
    source_kind: TaskSourceKind = "manual"
    source_ref: Optional[Dict[str, Any]] = None
    question: str = ""
    sql: str = ""
    status: TaskStatus = "todo"
    sort_order: int = 0
    last_result: Optional[Dict[str, Any]] = None
    session_id: str = ""
    created_at: str = ""
    updated_at: str = ""


class TaskCreate(BaseModel):
    project_id: int
    title: str = Field(min_length=1, max_length=512)
    ds_id: int = 0
    note: str = ""
    source_kind: TaskSourceKind = "manual"
    source_ref: Optional[Dict[str, Any]] = None
    question: str = ""
    sql: str = ""


class TaskUpdate(BaseModel):
    """编辑任务：只提交改动过的字段（未提交的保持原值）。"""
    title: Optional[str] = Field(default=None, min_length=1, max_length=512)
    note: Optional[str] = None
    ds_id: Optional[int] = None
    question: Optional[str] = None
    sql: Optional[str] = None
    status: Optional[Literal["todo", "done", "failed"]] = None


class TaskReorderRequest(BaseModel):
    project_id: int
    #: 调序后本项目清单的完整 id 顺序（↑↓ 按钮改一位也要提交整份）
    ids: List[int] = Field(default_factory=list)


class TaskRunRequest(BaseModel):
    project_id: int
    #: 留空 = 本项目全部未完成任务（没绑 SQL 的逐条给出跳过原因，不是静默忽略）
    ids: List[int] = Field(default_factory=list)
    #: 人工确认位：必须是界面勾选过的 true，否则 409。批量执行等于批量确认执行
    confirmed: bool = False


class TaskRunItem(BaseModel):
    """一条任务的执行回包。ran=False 时 reason 说明为什么没跑（不是失败）。"""
    id: int
    title: str
    ran: bool
    status: TaskStatus = "todo"
    reason: str = ""
    session_id: str = ""
    row_count: Optional[int] = None
    error: str = ""


class TaskRunResult(BaseModel):
    items: List[TaskRunItem] = Field(default_factory=list)
    ran_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    msg: str = ""


# ---------------------------------------------------------------- 通用响应
class OkResponse(BaseModel):
    success: bool = True
    msg: str = ""


class TestConnectionResponse(BaseModel):
    connected: bool
    msg: str


class SwitchResponse(BaseModel):
    success: bool = True
    ds_id: int
    msg: str = "已切换"
