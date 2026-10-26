/* 一轮分析的状态机 —— 五个阶段，每阶段可反复重跑。

   与后端链路一一对应（不是前端自己发明的流程）：
     parse    解析意图  → POST /parse_intent
     sql      生成 SQL  → POST /generate_sql
     confirm  人工确认  → POST /confirm_sql      （后端据此落 sql_confirmed）
     execute  执行查询  → POST /execute_sql
     analysis 分析输出  → POST /analysis

   两个关键设计：
   1) **每阶段有 attempts（尝试序列）**。退回某阶段改完再跑，会新增一次尝试，
      旧尝试保留可回看（标记 stale 表示"已不是当前路径"）。这就是"同一对话中
      的独立分支"的落点：分叉发生在阶段内，而不是复制整段对话。
   2) **重跑某阶段自动作废其后所有阶段**。改了 SQL 却还留着旧查询结果，
      是这类界面最容易出现的错，作废逻辑放在 reducer 里统一做，不靠调用方自觉。 */
import type {
  AnalysisResult, ChartKind, IntentConfirmCard, QueryResult, SqlSecurityResult,
} from '../api/types'

export type StageKey = 'parse' | 'sql' | 'confirm' | 'execute' | 'analysis'

export const STAGE_ORDER: StageKey[] = ['parse', 'sql', 'confirm', 'execute', 'analysis']

export const STAGE_META: Record<StageKey, { label: string; desc: string; action: string }> = {
  parse: {
    label: '解析意图',
    desc: '理解自然语言问题，锁定目标库表，产出意图卡片',
    action: '提问后自动执行',
  },
  sql: {
    label: '生成 SQL',
    desc: '依据意图生成只读 SQL，并通过安全校验',
    action: '确认意图后执行',
  },
  confirm: {
    label: '人工确认',
    desc: '你确认后才会真正访问数据库（后端二次确认，不可绕过）',
    action: '需你确认',
  },
  execute: {
    label: '执行查询',
    desc: '只读执行，带查询超时与行数封顶',
    action: '确认后自动执行',
  },
  analysis: {
    label: '分析输出',
    desc: '统计、规则洞察与图表推荐（纯计算）',
    action: '出结果后自动执行',
  },
}

export type StageStatus =
  | 'idle'      // 还没跑到
  | 'running'   // 请求进行中
  | 'ok'        // 已完成
  | 'await'     // 等你确认
  | 'failed'    // 失败/被安全门拦截
  | 'stale'     // 产出已失效（其前置阶段被重跑），需重跑

export type EventKind = 'think' | 'tool' | 'confirm' | 'output' | 'error'
export type EventStatus = 'running' | 'ok' | 'await' | 'failed'

/** 阶段内的过程消息：让"Agent 这一步具体在干什么"可见 */
export interface StageEvent {
  key: string
  kind: EventKind
  status: EventStatus
  title: string
  detail?: string[]
}

/** 一次尝试 = 该阶段的一次执行（含过程消息与产出快照） */
export interface StageAttempt {
  id: number
  status: StageStatus
  events: StageEvent[]
  startedAt: number
  finishedAt: number | null
  error: string | null
  edited: boolean                 // 本次是否用了手工编辑的输入
  note: string | null             // 如「使用手工修改版 SQL」
  intent?: IntentConfirmCard
  sql?: SqlSecurityResult
  query?: QueryResult
  analysis?: AnalysisResult
}

export interface StageState {
  attempts: StageAttempt[]
  view: number                    // 当前查看第几次尝试（下标）
}

/** 意图阶段可编辑的字段（回传给 generate_sql 的 intent_context 就是整张卡片） */
export interface IntentDraft {
  intent: string
  target_db: string
  target_tables: string[]
  target_fields: string[]
}

export interface AnalysisPrefs {
  chart: ChartKind
  rows: number                    // 明细表展示行数上限
}

export const ROW_LIMITS = [20, 100, 500, 2000] as const

export interface RoundState {
  sessionId: string
  /** 由「新建会话」落库的空会话 id；首次提问时绑定，绑定后即清空。
      没有它时每次提问由后端新建会话（历史里就不会有空白记录）。 */
  newSessionId: string | null
  question: string
  stages: Record<StageKey, StageState>
  focus: StageKey                 // 当前聚焦阶段
  /* 当前生效产出（= 各阶段最后一次尝试的产出） */
  intent: IntentConfirmCard | null
  sql: SqlSecurityResult | null
  query: QueryResult | null
  analysis: AnalysisResult | null
  /* 编辑草稿 */
  intentDraft: IntentDraft | null
  sqlDraft: string | null
  editing: StageKey | null        // 处于编辑态的阶段
  prefs: AnalysisPrefs
  busy: boolean
  error: string | null
}

const MAX_ATTEMPTS = 6            // 每阶段最多保留的尝试数，防止无限堆积

const emptyStages = (): Record<StageKey, StageState> => ({
  parse: { attempts: [], view: 0 },
  sql: { attempts: [], view: 0 },
  confirm: { attempts: [], view: 0 },
  execute: { attempts: [], view: 0 },
  analysis: { attempts: [], view: 0 },
})

export const initialRound: RoundState = {
  sessionId: '', newSessionId: null, question: '', stages: emptyStages(), focus: 'parse',
  intent: null, sql: null, query: null, analysis: null,
  intentDraft: null, sqlDraft: null, editing: null,
  prefs: { chart: 'bar', rows: 100 },
  busy: false, error: null,
}

export interface AttemptOutput {
  intent?: IntentConfirmCard
  sql?: SqlSecurityResult
  query?: QueryResult
  analysis?: AnalysisResult
}

export type RoundAction =
  | { type: 'reset' }
  | { type: 'ask'; question: string }
  | { type: 'session/new'; sessionId: string }
  | { type: 'focus'; stage: StageKey }
  | { type: 'attempt/start'; stage: StageKey; edited?: boolean; note?: string | null }
  | { type: 'attempt/event'; stage: StageKey; event: StageEvent }
  | { type: 'attempt/finish'; stage: StageKey; status: StageStatus;
      error?: string | null; output?: AttemptOutput }
  | { type: 'attempt/view'; stage: StageKey; index: number }
  | { type: 'draft/intent'; patch: Partial<IntentDraft> }
  | { type: 'draft/sql'; sql: string }
  | { type: 'edit'; stage: StageKey | null }
  | { type: 'prefs'; patch: Partial<AnalysisPrefs> }
  | { type: 'data'; patch: Partial<Pick<RoundState, 'sessionId' | 'newSessionId' | 'error'>> }

/** 某阶段重跑后，其后所有阶段的产出都不再可信 —— 统一作废，避免"改了 SQL 还看旧结果" */
function invalidateAfter(stages: Record<StageKey, StageState>, from: StageKey) {
  const start = STAGE_ORDER.indexOf(from) + 1
  const patch: Partial<Record<StageKey, StageState>> = {}
  for (let i = start; i < STAGE_ORDER.length; i += 1) {
    const key = STAGE_ORDER[i]
    patch[key] = {
      attempts: stages[key].attempts.map((a) => ({ ...a, status: 'stale' as StageStatus })),
      view: stages[key].view,
    }
  }
  return patch
}

/** 清空从某阶段起的所有生效产出（重跑前调用，保证界面不残留旧值） */
function clearOutputFrom(from: StageKey): Partial<RoundState> {
  const idx = STAGE_ORDER.indexOf(from)
  const patch: Partial<RoundState> = {}
  if (idx <= 0) patch.intent = null
  if (idx <= 1) patch.sql = null
  if (idx <= 3) patch.query = null
  if (idx <= 4) patch.analysis = null
  return patch
}

export function roundReducer(s: RoundState, a: RoundAction): RoundState {
  switch (a.type) {
    case 'reset':
      return initialRound

    case 'ask':
      return {
        ...initialRound,
        question: a.question,
        // 新会话要穿过这一次 ask（parse 阶段要用它绑定），不能在这里被重置掉
        newSessionId: s.newSessionId,
        stages: emptyStages(),
      }

    case 'session/new':
      // 新建空会话：清空当前轮的事件流，进入空态等提问
      return { ...initialRound, newSessionId: a.sessionId }

    case 'focus':
      return { ...s, focus: a.stage }

    case 'edit':
      return { ...s, editing: a.stage }

    case 'attempt/start': {
      const stage = s.stages[a.stage]
      const attempt: StageAttempt = {
        id: stage.attempts.length + 1,
        status: 'running',
        events: [],
        startedAt: Date.now(),
        finishedAt: null,
        error: null,
        edited: a.edited ?? false,
        note: a.note ?? null,
      }
      // 旧尝试标记 stale：它们仍是历史分支，但不再是当前路径
      const attempts = [
        ...stage.attempts.map((x) => ({ ...x, status: 'stale' as StageStatus })),
        attempt,
      ].slice(-MAX_ATTEMPTS)

      return {
        ...s,
        busy: true,
        error: null,
        stages: {
          ...s.stages,
          [a.stage]: { attempts, view: attempts.length - 1 },
          ...invalidateAfter(s.stages, a.stage),
        },
        /* 重新生成 SQL 时作废旧的 SQL 草稿：否则新生成的语句会被上次的手改版本
           挡住，界面显示的"待执行 SQL"并不是刚生成的那条。 */
        ...(a.stage === 'sql' ? { sqlDraft: null } : {}),
        ...clearOutputFrom(a.stage),
      }
    }

    case 'attempt/event': {
      const stage = s.stages[a.stage]
      const idx = stage.attempts.length - 1
      if (idx < 0) return s
      const attempts = stage.attempts.map((att, i) => {
        if (i !== idx) return att
        const exists = att.events.some((e) => e.key === a.event.key)
        return {
          ...att,
          events: exists
            ? att.events.map((e) => (e.key === a.event.key ? { ...e, ...a.event } : e))
            : [...att.events, a.event],
        }
      })
      return { ...s, stages: { ...s.stages, [a.stage]: { ...stage, attempts } } }
    }

    case 'attempt/finish': {
      const stage = s.stages[a.stage]
      const idx = stage.attempts.length - 1
      if (idx < 0) return s
      const attempts = stage.attempts.map((att, i) => (
        i === idx
          ? { ...att, ...a.output, status: a.status, finishedAt: Date.now(), error: a.error ?? null }
          : att
      ))
      const latest = attempts[idx]
      return {
        ...s,
        busy: a.status === 'running',
        stages: { ...s.stages, [a.stage]: { attempts, view: idx } },
        intent: latest.intent ?? s.intent,
        sql: latest.sql ?? s.sql,
        query: latest.query ?? s.query,
        analysis: latest.analysis ?? s.analysis,
      }
    }

    case 'attempt/view':
      return {
        ...s,
        stages: {
          ...s.stages,
          [a.stage]: { ...s.stages[a.stage], view: Math.max(0, Math.min(a.index, s.stages[a.stage].attempts.length - 1)) },
        },
      }

    case 'draft/intent':
      return {
        ...s,
        intentDraft: {
          intent: s.intentDraft?.intent ?? s.intent?.intent ?? '',
          target_db: s.intentDraft?.target_db ?? s.intent?.target_db ?? '',
          target_tables: s.intentDraft?.target_tables ?? s.intent?.target_tables ?? [],
          target_fields: s.intentDraft?.target_fields ?? s.intent?.target_fields ?? [],
          ...a.patch,
        },
      }

    case 'draft/sql':
      return { ...s, sqlDraft: a.sql }

    case 'prefs':
      return { ...s, prefs: { ...s.prefs, ...a.patch } }

    case 'data':
      return { ...s, ...a.patch }

    default:
      return s
  }
}

/** 意图草稿是否真的被改过：拿草稿与已确认卡片逐字段比，
    只看"草稿存不存在"会把"打开过编辑框但没改"也算成改过。 */
export function intentEdited(s: RoundState): boolean {
  const d = s.intentDraft
  const c = s.intent
  if (!d || !c) return false
  return d.intent !== c.intent
    || d.target_db !== c.target_db
    || d.target_tables.join(',') !== c.target_tables.join(',')
    || d.target_fields.join(',') !== c.target_fields.join(',')
}

/** 阶段当前状态 = 最后一次尝试的状态 */
export function stageStatus(s: RoundState, key: StageKey): StageStatus {
  const attempts = s.stages[key].attempts
  if (attempts.length === 0) return 'idle'
  return attempts[attempts.length - 1].status
}

/** 当前正在查看的尝试 */
export function viewing(s: RoundState, key: StageKey): StageAttempt | null {
  const stage = s.stages[key]
  return stage.attempts[stage.view] ?? null
}
