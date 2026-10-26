/* 后端接口清单（唯一出口）。
   路径与 basic/api/*.py 的 router 一一对应，改后端接口时只动这个文件。 */
import { http, upload } from './http'
import type {
  AgentSession, AnalysisBody, AnalysisResult, Datasource, DatasourceCreate,
  DatasourceUpdate, GenerateSqlBody, HealthResponse, IntentConfirmCard,
  OkResponse, ParseIntentBody, Project, Provider, ProviderModelItem,
  QueryResult, RemoteModelsResponse, RoundSummary, SampleResult,
  SourceInsightResult, SqlAdviceResult, SqlSecurityResult, SwitchResponse,
  TableSchema, TableStats, Task, TaskCreate, TaskRunResult, TaskUpdate,
  TestConnectionResponse,
} from './types'

/* ---------------- 运维 ---------------- */
export const opsApi = {
  health: () => http.get<HealthResponse>('/healthz', { timeoutMs: 8000 }),
}

/* ---------------- 数据源 ---------------- */
export const datasourceApi = {
  list: () => http.get<Datasource[]>('/api/datasource'),
  create: (body: DatasourceCreate) => http.post<Datasource>('/api/datasource', body),
  update: (id: number, body: DatasourceUpdate) => http.put<Datasource>(`/api/datasource/${id}`, body),
  remove: (id: number) => http.del<OkResponse>(`/api/datasource/${id}`),
  test: (id: number) => http.post<TestConnectionResponse>(`/api/datasource/${id}/test`),
  switchTo: (id: number) => http.post<SwitchResponse>(`/api/datasource/${id}/switch`),
  importFile: (file: File) => upload<Datasource>('/api/datasource/file', file),
}

/* ---------------- 元数据探查 ---------------- */
export const metaApi = {
  databases: (dsId?: number | null) =>
    http.get<{ databases: string[] }>(`/api/meta/databases${qs({ ds_id: dsId })}`),
  tables: (db: string, dsId?: number | null) =>
    http.get<{ database: string; tables: string[] }>(
      `/api/meta/${encodeURIComponent(db)}/tables${qs({ ds_id: dsId })}`),
  schema: (db: string, table: string, dsId?: number | null) =>
    http.get<TableSchema>(
      `/api/meta/${encodeURIComponent(db)}/table/${encodeURIComponent(table)}/schema${qs({ ds_id: dsId })}`),
  sample: (db: string, table: string, limit = 10, dsId?: number | null) =>
    http.get<SampleResult>(
      `/api/meta/${encodeURIComponent(db)}/table/${encodeURIComponent(table)}/sample${qs({ limit, ds_id: dsId })}`),
  stats: (db: string, table: string, dsId?: number | null) =>
    http.get<TableStats>(
      `/api/meta/${encodeURIComponent(db)}/table/${encodeURIComponent(table)}/stats${qs({ ds_id: dsId })}`),
}

/* ---------------- Agent 五步链路 ---------------- */
export const agentApi = {
  parseIntent: (body: ParseIntentBody) =>
    http.post<IntentConfirmCard>('/api/agent/parse_intent', body),
  generateSql: (body: GenerateSqlBody) =>
    http.post<SqlSecurityResult>('/api/agent/generate_sql', body),
  confirmSql: (sessionId: string) =>
    http.post<OkResponse>('/api/agent/confirm_sql', { session_id: sessionId }),
  /* sql 传空串 = 执行会话内已确认的那条；传非空 = 执行用户手工修改后的版本
     （后端仍要求 session_id + sql_confirmed + 二次安全校验，绕不过确认流程）。
     执行成功后后端会把这条 SQL 回写进会话，保证回看可溯源。 */
  executeSql: (sessionId: string, sql = '') =>
    http.post<QueryResult>('/api/agent/execute_sql', { session_id: sessionId, sql }),
  analysis: (body: AnalysisBody) =>
    http.post<AnalysisResult>('/api/agent/analysis', body),
  session: (sessionId: string) => http.get<AgentSession>(`/api/agent/session/${sessionId}`),
  /* 新建一条空会话（无问题）：之后首次提问时由 parseIntent 带 session_id 绑定，
     而不是每次提问都新开一条（否则历史里会堆满空记录）。 */
  newSession: (dsId?: number | null, projectId?: number | null) =>
    http.post<{ session_id: string; ds_id: number; project_id: number; created: boolean }>(
      '/api/agent/sessions', { ds_id: dsId ?? null, project_id: projectId ?? null }),
  /* SQL 优化：分析一条（通常是手工改过的）SQL。narrate=false 只跑规则引擎，
     即时且不花 token；点「AI 解读」时才带 narrate=true。 */
  sqlAdvice: (body: { sql: string; ds_id?: number | null; session_id?: string; narrate?: boolean }) =>
    http.post<SqlAdviceResult>('/api/agent/sql_advice', body),
  /* 数据源系统性描述与优化建议（第一阶段）。后端按 (数据源, 库, 是否叙述) 缓存，
     refresh=true 强制重新剖析。
     narrate=false 只跑规则层（实测 1~2 秒）；narrate=true 才调大模型把事实讲成
     业务场景 —— 实测那份 JSON 要 150 秒以上才生成完，所以这条的超时单独放宽，
     而且前端只在后台取它，不让第一步干等（默认 60 秒超时会把它判死，
     结果是每次剖析都白等两分钟再回落成规则结论）。 */
  sourceInsight: (body: { ds_id?: number | null; db?: string; tables?: string[];
                          refresh?: boolean; narrate?: boolean }) =>
    http.post<SourceInsightResult>('/api/agent/source_insight', body,
                                   { timeoutMs: body.narrate ? 300_000 : 60_000 }),
}

/* ---------------- 轮次历史 ---------------- */
export const roundsApi = {
  list: (limit = 200, dsId?: number | null) =>
    http.get<{ rounds: RoundSummary[] }>(`/api/agent/rounds${qs({ limit, ds_id: dsId })}`),
  get: (sessionId: string) => http.get<AgentSession>(`/api/agent/rounds/${sessionId}`),
  /** 重命名会话（不动 user_question，原始提问要留痕） */
  rename: (sessionId: string, title: string) =>
    http.patch<{ success: boolean; title: string }>(
      `/api/agent/rounds/${sessionId}`, { title }),
  /** 把会话移到另一个项目 */
  move: (sessionId: string, projectId: number) =>
    http.patch<{ success: boolean; project_id: number }>(
      `/api/agent/rounds/${sessionId}/project`, { project_id: projectId }),
  remove: (sessionId: string) => http.del<OkResponse>(`/api/agent/rounds/${sessionId}`),
}

/* ---------------- 模型供应商 ---------------- */
export interface ProviderCreateBody {
  provider_key: string
  name: string
  api_base: string
  api_key: string
  protocol?: string
  enabled?: boolean
  models?: string[]
}

export const providersApi = {
  list: () => http.get<Provider[]>('/api/providers'),
  active: () => http.get<{ provider: Provider | null; model: ProviderModelItem | null }>(
    '/api/providers/active'),
  create: (body: ProviderCreateBody) => http.post<Provider>('/api/providers', body),
  update: (id: number, body: Partial<ProviderCreateBody>) =>
    http.put<Provider>(`/api/providers/${id}`, body),
  remove: (id: number) => http.del<OkResponse>(`/api/providers/${id}`),
  /* 表单未保存时的草稿测试：api_key 留空且带 provider_id 会复用已存密钥 */
  testDraft: (body: { api_base: string; api_key?: string; protocol?: string; model?: string; provider_id?: number }) =>
    http.post<TestConnectionResponse>('/api/providers/test', body, { timeoutMs: 40000 }),
  test: (id: number, model = '') =>
    http.post<TestConnectionResponse>(
      `/api/providers/${id}/test${model ? `?model=${encodeURIComponent(model)}` : ''}`,
      {}, { timeoutMs: 40000 }),
  addModel: (id: number, modelName: string) =>
    http.post<Provider>(`/api/providers/${id}/models`, { model_name: modelName }),
  /* 拉取供应商侧模型清单（列表里有 ≠ 有调用权限，登记后仍需逐个测试） */
  remoteModels: (id: number) =>
    http.get<RemoteModelsResponse>(`/api/providers/${id}/remote-models`, { timeoutMs: 20000 }),
  addModelsBatch: (id: number, modelNames: string[]) =>
    http.post<Provider>(`/api/providers/${id}/models/batch`, { model_names: modelNames }),
  removeModel: (id: number, modelId: number) =>
    http.del<Provider>(`/api/providers/${id}/models/${modelId}`),
  activateModel: (id: number, modelId: number) =>
    http.post<Provider>(`/api/providers/${id}/models/${modelId}/activate`, {}),
}

/* ---------------- 项目（会话分组，每个项目一个本地文件夹） ---------------- */
export const projectsApi = {
  list: () => http.get<Project[]>('/api/projects'),
  create: (body: { name?: string; folder_path?: string }) =>
    http.post<Project>('/api/projects', body),
  update: (id: number, body: { name?: string; folder_path?: string }) =>
    http.put<Project>(`/api/projects/${id}`, body),
  remove: (id: number) => http.del<OkResponse>(`/api/projects/${id}`),
}

/* ---------------- 任务清单（按项目一份，来自分析建议 + 手工添加） ---------------- */
export const tasksApi = {
  list: (projectId?: number | null) =>
    http.get<Task[]>(`/api/tasks${qs({ project_id: projectId })}`),
  create: (body: TaskCreate) => http.post<Task>('/api/tasks', body),
  update: (id: number, body: TaskUpdate) => http.put<Task>(`/api/tasks/${id}`, body),
  remove: (id: number) => http.del<OkResponse>(`/api/tasks/${id}`),
  /** 提交调序后的完整 id 顺序（↑↓ 每点一次提交整份，避免逐条 +1/-1 的并发错位） */
  reorder: (projectId: number, ids: number[]) =>
    http.post<Task[]>('/api/tasks/reorder', { project_id: projectId, ids }),
  /* 批量执行：confirmed 必须是人在界面勾选过的确认位，后端不认裸调用。
     串起来跑最慢的一条也可能等满查询超时，超时给到 3 分钟。 */
  run: (projectId: number, ids: number[], confirmed: boolean) =>
    http.post<TaskRunResult>('/api/tasks/run',
      { project_id: projectId, ids, confirmed }, { timeoutMs: 180000 }),
}

function qs(params: Record<string, unknown>): string {
  const usp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    usp.set(k, String(v))
  }
  const s = usp.toString()
  return s ? `?${s}` : ''
}
