/* 与后端 basic/models/pydantic_schemas.py 一一对应的 TS 类型。
   字段命名保持后端原样（snake_case），不做"前端友好"改名 ——
   改名就会引入映射层，映射层就是之前前端"字段对不上"的根源。 */

/* ---------------- 6.1 数据源 ---------------- */
export interface Datasource {
  id: number
  name: string
  type: 'mysql' | 'file'
  host: string | null
  port: number | null
  user: string | null
  password: string | null
  database: string | null
  is_active: boolean
  password_set: boolean
  /** 该源允许的最高写档位；后端默认 read，文件源恒为 read（SQLite 以 mode=ro 打开） */
  allowed_ops: PermTier
  created_at: string
}

export interface DatasourceCreate {
  name: string
  type?: 'mysql' | 'file'
  host?: string
  port?: number
  user?: string
  password?: string
  database?: string
  allowed_ops?: PermTier
}

export interface DatasourceUpdate {
  name?: string
  host?: string
  port?: number
  user?: string
  password?: string   // 传空串表示保持原密码
  database?: string
  allowed_ops?: PermTier
}

/* ---------------- 6.2 会话状态 ---------------- */
export interface AgentSession {
  session_id: string
  ds_id: number
  project_id: number
  title: string
  user_question: string
  intent: string
  target_db: string
  intent_confirmed: boolean
  sql: string | null
  sql_confirmed: boolean
  query_result: QueryResult | null
  analysis_result: AnalysisResult | null
  created_at: string
  updated_at: string
}

/* ---------------- 6.3 意图确认卡片 ---------------- */
export interface IntentConfirmCard {
  original_question: string
  intent: string
  target_db: string
  target_tables: string[]
  target_fields: string[]
  sql_preview: string
  risk_level: 'low' | 'medium'
  hint: string
  session_id: string
}

/* ---------------- 6.4 SQL 校验结果 ---------------- */
export interface SqlSecurityResult {
  passed: boolean
  reason: string | null
  final_sql: string | null
}

/* ---------------- 6.7 模型供应商 ---------------- */
export interface ProviderModelItem {
  id: number
  model_name: string
  is_default: boolean
  is_active: boolean
}

export interface Provider {
  id: number
  provider_key: string
  name: string
  api_base: string
  protocol: string
  enabled: boolean
  has_key: boolean
  models: ProviderModelItem[]
  created_at: string
  updated_at: string
}

/** 供应商侧拉回的模型清单（supported=false 表示端点不提供该列表） */
export interface RemoteModelsResponse {
  supported: boolean
  models: string[]
  msg: string
}

/** 供应商预设模板：只回填前端表单，不落库 */
export interface ProviderTemplate {  key: string
  label: string
  providerKey: string
  name: string
  apiBase: string
  protocol: string
  /** 接口地址需要用户自己填（如 MiMo）时为 true，表单上给出提示 */
  needBase?: boolean
}

/* ---------------- 6.9 项目 ---------------- */
export interface Project {
  id: number
  name: string
  folder_path: string
  session_count: number
  is_default: boolean
  created_at: string
  updated_at: string
}

/* ---------------- 6.10 任务清单 ---------------- */
/** running 只由后端批量执行过程写入，界面只在其余三者间切换 */
export type TaskStatus = 'todo' | 'running' | 'done' | 'failed'
/** manual=自己加的；advice=数据源剖析建议；sql_advice=SQL 优化建议 */
export type TaskSourceKind = 'manual' | 'advice' | 'sql_advice'

export interface Task {
  id: number
  project_id: number
  ds_id: number
  title: string
  note: string
  source_kind: TaskSourceKind
  /** 加入清单那一刻的证据快照（表/规则码/依据原文），用于长期可解释 */
  source_ref: Record<string, unknown> | null
  question: string
  /** 空串 = 这条只能人工办结，批量执行会明确跳过它 */
  sql: string
  status: TaskStatus
  sort_order: number
  last_result: Record<string, unknown> | null
  session_id: string
  created_at: string
  updated_at: string
}

export interface TaskCreate {
  project_id: number
  title: string
  ds_id?: number
  note?: string
  source_kind?: TaskSourceKind
  source_ref?: Record<string, unknown> | null
  question?: string
  sql?: string
}

export interface TaskUpdate {
  title?: string
  note?: string
  ds_id?: number
  question?: string
  sql?: string
  /** running 由后端执行过程写入，界面只在其余三者间切换 */
  status?: Extract<TaskStatus, 'todo' | 'done' | 'failed'>
}

export interface TaskRunItem {
  id: number
  title: string
  ran: boolean
  status: TaskStatus
  /** ran=false 时说明为什么没跑（不是失败） */
  reason: string
  session_id: string
  row_count: number | null
  error: string
}

export interface TaskRunResult {
  items: TaskRunItem[]
  ran_count: number
  skipped_count: number
  failed_count: number
  msg: string
}

/* ---------------- 写通道（后端 pydantic_schemas 6.10） ---------------- */
/** 权限档位：只读 < 写数据(DML) < 改结构(DDL)。请求档位只能等于或低于数据源上限。 */
export type PermTier = 'read' | 'write_dml' | 'write_ddl'

export interface WritePlanTarget {
  database: string
  table: string
  is_main: boolean
}

/** /prepare_write 的出参：passed=false 时 reason 就是拦人的那条规则，前端不二次猜测 */
export interface WritePlan {
  passed: boolean
  reason: string | null
  statement_kind: string
  statement_label: string
  targets: WritePlanTarget[]
  destructive: boolean
  confirm_phrase: string
  backup_table: string | null
  backup_sql: string | null
  estimate_sql: string | null
  needs_affected_rows: boolean
  final_sql: string | null
  estimated_rows: number | null
  requested_tier: PermTier
  effective_tier: PermTier
  max_allowed_rows: number
}

export interface WriteExecuteResult {
  statement_kind: string
  statement_label: string
  targets: WritePlanTarget[]
  destructive: boolean
  estimated_rows: number | null
  affected_rows: number
  cost_ms: number
  backup_table: string | null
  backup_rows: number | null
  rollback_sql: string | null
  final_sql: string
  /** 实际执行时生效的档位（被数据源上限压过就与请求档位不同） */
  effective_tier: PermTier
  audit_id: number
}

export interface WriteAuditItem {
  id: number
  session_id: string
  ds_id: number
  ds_name: string
  target_db: string
  statement_kind: string
  sql: string
  requested_tier: string
  effective_tier: string
  confirmed: boolean
  destructive: boolean
  estimated_rows: number | null
  affected_rows: number | null
  backup_table: string
  rollback_sql: string
  status: 'executed' | 'rejected' | 'failed' | string
  error: string
  cost_ms: number
  created_at: string
}

export interface WriteRequestBody {
  sql: string
  ds_id?: number | null
  db: string
  tier: PermTier
  session_id?: string
}

/* ---------------- 6.8 优化建议 ---------------- */
export type Severity = 'high' | 'medium' | 'low' | 'info'

export interface Advice {
  code: string
  severity: Severity
  title: string
  detail: string
  suggestion: string
  evidence: string
}

export interface TableFact {
  table: string
  row_estimate: number | null
  indexes: { name: string; columns: string[]; unique: boolean; primary: boolean }[]
  column_count: number
}

export interface SqlAdviceResult {
  sql: string
  readable: boolean
  parse_error: string | null
  advices: Advice[]
  facts: TableFact[]
  summary: string
  model_label: string
  llm_used: boolean
}

export interface SourceTableInsight {
  table: string
  row_estimate: number | null
  columns: { name: string; type: string; primary_key: boolean }[]
  indexes: { name: string; columns: string[]; unique: boolean; primary: boolean }[]
  advices: Advice[]
  role: string
}

export interface SourceInsightResult {
  database: string
  table_count: number
  total_row_estimate: number
  overview: string
  phenomena: string[]
  tables: SourceTableInsight[]
  advices: Advice[]
  table_roles: Record<string, string>
  optimization_plan: { priority: number; title: string; why: string; how: string }[]
  query_tips: string[]
  model_label: string
  llm_used: boolean
  from_cache: boolean
  note: string
}

/* ---------------- 6.5 查询结果 ---------------- */
export interface QueryResult {
  columns: string[]
  rows: Record<string, unknown>[]
  row_count: number
  cost_ms: number
  truncated: boolean
}

/* ---------------- 6.6 分析输出 ---------------- */
export type InsightType = 'anomaly' | 'trend' | 'comparison'
export type ChartKind = 'table' | 'bar' | 'line' | 'pie'

export interface Insight {
  type: InsightType
  title: string
  detail: string
  evidence: string
}

export interface AnalysisResult {
  summary_text: string
  stats: Record<string, unknown>
  insights: Insight[]
  suggest_chart: ChartKind
  chart_option: Record<string, unknown>
}

/* ---------------- 轮次历史（store.list_rounds） ---------------- */
export interface RoundSummary {
  session_id: string
  ds_id: number
  project_id: number
  title: string
  user_question: string
  intent: string
  target_db: string
  sql: string | null
  row_count: number | null
  cost_ms: number | null
  suggest_chart: ChartKind
  summary_text: string
  has_result: boolean
  created_at: string
  updated_at: string
}

/* ---------------- 元数据 ---------------- */
export interface ColumnMeta {
  name: string
  type: string
  comment: string
  primary_key: boolean
  nullable: boolean
}

export interface TableSchema {
  database: string
  table: string
  columns: ColumnMeta[]
  row_estimate: number | null
}

export interface ColumnStats {
  column: string
  null_count: number
  null_ratio: number
  distinct_count: number | null
  min_value: unknown
  max_value: unknown
  top_values: Record<string, unknown>[]
}

export interface TableStats {
  database: string
  table: string
  row_count: number
  columns: ColumnStats[]
}

export interface SampleResult {
  database: string
  table: string
  rows: Record<string, unknown>[]
  row_count: number
}

/* ---------------- 通用响应 ---------------- */
export interface OkResponse { success: boolean; msg: string }
export interface TestConnectionResponse { connected: boolean; msg: string }
export interface SwitchResponse { success: boolean; ds_id: number; msg: string }
export interface HealthResponse {
  status: string
  llm: string
  store: string
  /** 写总闸（AGENT_ALLOW_WRITES）：false 时任何写语句都被拒，档位选到多高都没用 */
  writes_enabled: boolean
  /** 单次写入允许的影响行数上限（WRITE_MAX_AFFECTED_ROWS） */
  write_max_affected_rows: number
}

/* ---------------- 请求体 ---------------- */
export interface ParseIntentBody {
  ds_id?: number | null
  user_query: string
  selected_db?: string
  selected_tables?: string[]
  selected_fields?: string[]
  /** 绑定到「新建会话」落下的空会话（须为未提问的会话） */
  session_id?: string
  /** 新建会话时归属的项目；缺省归入「未分组」 */
  project_id?: number | null
}

export interface GenerateSqlBody {
  session_id: string
  intent_context: Record<string, unknown>
}

export interface AnalysisBody {
  query_result: QueryResult
  session_id: string
  intent: string
}
