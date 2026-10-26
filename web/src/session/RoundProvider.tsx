/* 一轮分析的编排：把后端五步链路拆成五个可独立进入、可退回、可重跑的阶段。

   流程与后端落库状态严格对应（见 machine.ts 顶部注释）。这里只负责"怎么串"：
     ask()          → 跑 parse，停在「等你确认意图」
     confirmIntent()→ 跑 sql，停在「等你确认 SQL」（可用编辑后的意图重跑）
     confirmSql()   → 跑 confirm → execute → analysis（可携带手工改过的 SQL）

   每一次阶段执行都是一次 attempt：改了意图再生成、改了 SQL 再执行，
   都会新增一次尝试，旧尝试保留可回看 —— 这就是"同一对话内的分支"。 */
import {
  createContext, useCallback, useContext, useMemo, useReducer, useRef, type ReactNode,
} from 'react'
import { agentApi } from '../api'
import { errMessage } from '../hooks/useAsync'
import { useApp } from '../state/AppProvider'
import {
  initialRound, intentEdited, roundReducer, STAGE_ORDER,
  type AttemptOutput, type RoundState, type StageKey, type StageStatus,
} from './machine'

interface RunOptions {
  edited?: boolean
  note?: string | null
  customSql?: string        // 执行阶段：用手工修改后的 SQL
  intentCard?: Record<string, unknown>  // SQL 阶段：用手工修改后的意图卡片
}

interface RoundValue {
  state: RoundState
  ask: (question: string) => Promise<void>
  /** 新建一条空会话（落库），进入空态；之后首次提问绑定到它 */
  newSession: () => Promise<void>
  confirmIntent: (editedCard?: Record<string, unknown>) => Promise<void>
  confirmSql: (customSql?: string) => Promise<void>
  rerun: (stage: StageKey) => Promise<void>
  goto: (stage: StageKey) => void
  startEdit: (stage: StageKey | null) => void
  setIntentDraft: (patch: Partial<{ intent: string; target_db: string;
    target_tables: string[]; target_fields: string[] }>) => void
  setSqlDraft: (sql: string) => void
  setPrefs: (patch: Partial<{ chart: RoundState['prefs']['chart']; rows: number }>) => void
  viewAttempt: (stage: StageKey, index: number) => void
  reset: () => void
}

const RoundCtx = createContext<RoundValue | undefined>(undefined)

/** 阶段"主动终止"信号：与失败的区别是它有产出要留在界面上。
    典型场景：SQL 被安全门拦截 —— 拦截原因和原始 SQL 都要展示，
    但不能当成异常丢弃，也不能让外层把它覆盖成"成功"。 */
class StageStop extends Error {
  constructor(readonly stopStatus: StageStatus, readonly output: AttemptOutput) {
    super('stage-stop')
    this.name = 'StageStop'
  }
}

export function RoundProvider({ children }: { children: ReactNode }) {
  const app = useApp()
  const [state, dispatch] = useReducer(roundReducer, initialRound)

  /* 编排函数里要读到最新状态（async 中间态），用 ref 拿，
     避免把 state 放进依赖导致回调每次重建 */
  const stateRef = useRef(state)
  stateRef.current = state

  const busyRef = useRef(false)
  busyRef.current = state.busy

  const { selection, activeDs, refreshRounds } = app

  /* 执行一次阶段尝试：统一的 start → events → finish/failed 封装。
     返回产出（失败返回 null），调用方据此决定是否继续串联下一阶段。 */
  const runAttempt = useCallback(async (
    stage: StageKey,
    fn: () => Promise<AttemptOutput>,
    status: Extract<StageStatus, 'ok' | 'await'> = 'ok',
    opts?: RunOptions,
  ): Promise<AttemptOutput | null> => {
    dispatch({ type: 'attempt/start', stage, edited: opts?.edited, note: opts?.note })
    try {
      const out = await fn()
      dispatch({ type: 'attempt/finish', stage, status, output: out })
      return out
    } catch (e: unknown) {
      if (e instanceof StageStop) {
        dispatch({ type: 'attempt/finish', stage, status: e.stopStatus, output: e.output })
        return null
      }
      const msg = errMessage(e)
      dispatch({ type: 'attempt/event', stage, event: {
        key: 'err', kind: 'error', status: 'failed', title: STAGE_FAIL_TITLE[stage], detail: [msg],
      } })
      dispatch({ type: 'attempt/finish', stage, status: 'failed', error: msg })
      dispatch({ type: 'data', patch: { error: msg } })
      return null
    }
  }, [])

  /** 事件简写：往当前 attempt 里追加/更新一条过程消息 */
  const ev = (stage: StageKey, event: {
    key: string; kind: 'think' | 'tool' | 'confirm' | 'output' | 'error'
    status: 'running' | 'ok' | 'await' | 'failed'; title: string; detail?: string[]
  }) => dispatch({ type: 'attempt/event', stage, event })

  /** 新建空会话：先落库（用户确认的方案），再清空当前轮进入空态。
     后端要求绑定必须落在"没有问题"的会话上，所以失败时给出明确原因。 */
  const newSession = useCallback(async () => {
    if (!activeDs) throw new Error('尚未激活数据源')
    const r = await agentApi.newSession(activeDs.id, app.projectId)
    dispatch({ type: 'session/new', sessionId: r.session_id })
    app.setDraft('')
    refreshRounds()
    app.projects.reload()
  }, [activeDs, app, refreshRounds])

  /* ---------------------------------------------------------------- 阶段 1 */
  const ask = useCallback(async (question: string) => {
    const q = question.trim()
    if (!q || busyRef.current) return
    if (!activeDs) throw new Error('尚未激活数据源')

    dispatch({ type: 'ask', question: q })
    dispatch({ type: 'focus', stage: 'parse' })

    await runAttempt('parse', async () => {
      ev('parse', { key: 'think', kind: 'think', status: 'running',
        title: '解析问题意图', detail: ['读取库表结构，交给意图解析器'] })
      /* 有"新建会话"落下的空会话就绑定它，否则由后端新建 —— 这样历史里
         不会出现一堆没有问题的空记录（空记录只在用户主动新建时产生）。 */
      const pendingSid = stateRef.current.newSessionId || undefined
      const card = await agentApi.parseIntent({
        ds_id: activeDs.id,
        user_query: q,
        session_id: pendingSid,
        project_id: app.projectId,
        selected_db: selection.db,
        selected_tables: selection.tables,
        selected_fields: selection.fields,
      })
      dispatch({ type: 'data', patch: { sessionId: card.session_id, newSessionId: null } })
      ev('parse', { key: 'think', kind: 'think', status: 'ok', title: '解析问题意图',
        detail: [`意图：${card.intent}`, `目标库：${card.target_db}`,
                 `风险等级：${card.risk_level === 'medium' ? '中' : '低'}`] })
      ev('parse', { key: 'schema', kind: 'tool', status: 'ok', title: '检索库表上下文',
        detail: [
          card.target_tables.length ? `目标表：${card.target_tables.join('、')}` : '未锁定具体表',
          card.target_fields.length ? `目标字段：${card.target_fields.join('、')}` : '未锁定具体字段',
          selection.tables.length ? `（你已选 ${selection.tables.length} 张表）` : '',
        ].filter(Boolean) })
      refreshRounds()
      return { intent: card }
    }, 'await')          // parse 停在"等你确认意图"
    /* 注意依赖里必须带 app.projectId：它参与组装请求体，
       漏了就会一直用首次渲染时的 null，会话永远落回「未分组」（踩过）。 */
  }, [activeDs, selection, refreshRounds, runAttempt, app.projectId])

  /* ---------------------------------------------------------------- 阶段 2 */
  const runSql = useCallback(async (card?: Record<string, unknown>, edited = false) => {
    dispatch({ type: 'focus', stage: 'sql' })
    const out = await runAttempt('sql', async () => {
      ev('sql', { key: 'gen', kind: 'tool', status: 'running',
        title: '生成 SQL 并做安全校验', detail: ['只读规则校验 · 自动追加 LIMIT'] })
      const result = await agentApi.generateSql({
        session_id: stateRef.current.sessionId,
        intent_context: card ?? buildContext(stateRef.current.intent),
      })
      if (!result.passed) {
        ev('sql', { key: 'gen', kind: 'tool', status: 'failed',
          title: 'SQL 安全校验拦截', detail: [result.reason || '安全规则拦截，未生成可执行 SQL'] })
        dispatch({ type: 'data', patch: { error: result.reason || 'SQL 安全校验未通过' } })
        // 拦截：停在这里，不往后串联（外层 runAttempt 见到 StageStop 会保留产出与失败态）
        throw new StageStop('failed', { sql: result })
      }
      ev('sql', { key: 'gen', kind: 'tool', status: 'ok',
        title: 'SQL 生成完成 · 安全校验通过',
        detail: ['仅 SELECT 只读', `语句长度 ${(result.final_sql || '').length} 字符`] })
      return { sql: result }
    }, 'ok', edited ? { edited: true, note: '使用手工编辑后的意图' } : undefined)

    // 拦截或失败时不进确认阶段（runAttempt 返回 null）
    if (!out) return

    // 生成成功 → 停在人工确认阶段等你确认
    dispatch({ type: 'focus', stage: 'confirm' })
    runAttempt('confirm', async () => {
      ev('confirm', { key: 'wait', kind: 'confirm', status: 'await',
        title: '等你确认后才会执行',
        detail: ['后端会记录 sql_confirmed，未确认时执行接口直接返回 409'] })
      return {}
    }, 'await')
  }, [runAttempt])

  /** 确认意图 → 生成 SQL。意图被改过时用草稿，并给这次尝试打上"手工编辑"标记 */
  const confirmIntent = useCallback(async () => {
    const s = stateRef.current
    const edited = intentEdited(s)
    await runSql(buildContext(s.intent, edited ? s.intentDraft ?? undefined : undefined), edited)
  }, [runSql])

  /* ------------------------------------------------- 阶段 3 → 4 → 5 串联 */
  const confirmSql = useCallback(async (customSql?: string) => {
    const sid = stateRef.current.sessionId
    if (!sid) return

    // ③ 人工确认
    const ok = await runAttempt('confirm', async () => {
      ev('confirm', { key: 'wait', kind: 'confirm', status: 'running', title: '写入二次确认状态' })
      await agentApi.confirmSql(sid)
      ev('confirm', { key: 'wait', kind: 'confirm', status: 'ok',
        title: 'SQL 已确认', detail: ['已落 sql_confirmed，可以执行'] })
      return {}
    }, 'ok', customSql ? { edited: true, note: '使用手工修改版 SQL' } : undefined)
    if (!ok) return

    // ④ 执行查询
    const out = await runAttempt('execute', async (): Promise<AttemptOutput> => {
      ev('execute', { key: 'run', kind: 'tool', status: 'running',
        title: '执行只读查询', detail: ['查询超时保护 · 只读连接 · 行数封顶'] })
      const query = await agentApi.executeSql(sid, customSql ?? '')
      ev('execute', { key: 'run', kind: 'tool', status: 'ok', title: '查询完成',
        detail: [`返回 ${query.row_count} 行 × ${query.columns.length} 列`,
                 `耗时 ${query.cost_ms} ms`,
                 query.truncated ? '结果已触达行数上限被截断' : ''].filter(Boolean) })
      return { query }
    }, 'ok', customSql ? { edited: true, note: '使用手工修改版 SQL' } : undefined)
    if (!out?.query) return

    // ⑤ 分析输出
    await runAttempt('analysis', async (): Promise<AttemptOutput> => {
      ev('analysis', { key: 'out', kind: 'output', status: 'running',
        title: '生成分析结论', detail: ['统计 + 规则洞察 + 图表推荐'] })
      const analysis = await agentApi.analysis({
        query_result: out.query!,
        session_id: sid,
        intent: stateRef.current.intent?.intent || '',
      })
      ev('analysis', { key: 'out', kind: 'output', status: 'ok', title: '分析结论',
        detail: [analysis.summary_text] })
      refreshRounds()
      return { analysis }
    }, 'ok', customSql ? { edited: true, note: '使用手工修改版 SQL' } : undefined)

    dispatch({ type: 'focus', stage: 'analysis' })
  }, [runAttempt, refreshRounds])

  /** 重跑某个阶段（用于"退回后重跑"）：按阶段决定从哪一步开始串 */
  const rerun = useCallback(async (stage: StageKey) => {
    const s = stateRef.current
    switch (stage) {
      case 'parse':
        /* 重新解析意图 = 新建会话（后端没有"用新问题更新已有会话"的接口），
           新旧会话混在一条事件流里必然错乱。这里的"退回"= 把问题回填输入框、
           结束本轮，由你改完重新提问，历史里才是干净的一条。 */
        app.setDraft(s.question)
        dispatch({ type: 'reset' })
        break
      case 'sql': {
        const edited = intentEdited(s)
        await runSql(buildContext(s.intent, edited ? s.intentDraft ?? undefined : undefined), edited)
        break
      }
      case 'confirm':
      case 'execute':
      case 'analysis':
        await confirmSql(s.sqlDraft ?? undefined)
        break
      default:
        break
    }
  }, [ask, runSql, confirmSql])

  const reset = useCallback(() => dispatch({ type: 'reset' }), [])

  const value = useMemo<RoundValue>(() => ({
    state,
    ask, newSession, confirmIntent, confirmSql, rerun, reset,
    goto: (stage) => dispatch({ type: 'focus', stage }),
    startEdit: (stage) => dispatch({ type: 'edit', stage }),
    setIntentDraft: (patch) => dispatch({ type: 'draft/intent', patch }),
    setSqlDraft: (sql) => dispatch({ type: 'draft/sql', sql }),
    setPrefs: (patch) => dispatch({ type: 'prefs', patch }),
    viewAttempt: (stage, index) => dispatch({ type: 'attempt/view', stage, index }),
  }), [state, ask, confirmIntent, confirmSql, rerun, reset])

  return <RoundCtx.Provider value={value}>{children}</RoundCtx.Provider>
}

const STAGE_FAIL_TITLE: Record<StageKey, string> = {
  parse: '意图解析失败',
  sql: 'SQL 生成失败',
  confirm: '确认失败',
  execute: '查询执行失败',
  analysis: '分析失败',
}

/** generate_sql 要整张意图卡：编辑时用草稿覆盖卡片对应字段 */
function buildContext(
  card: RoundState['intent'],
  draft?: { intent?: string; target_db?: string; target_tables?: string[]; target_fields?: string[] },
): Record<string, unknown> {
  return {
    original_question: card?.original_question ?? '',
    intent: draft?.intent ?? card?.intent ?? '',
    target_db: draft?.target_db ?? card?.target_db ?? '',
    target_tables: draft?.target_tables ?? card?.target_tables ?? [],
    target_fields: draft?.target_fields ?? card?.target_fields ?? [],
    sql_preview: card?.sql_preview ?? '',
    risk_level: card?.risk_level ?? 'low',
    hint: card?.hint ?? '',
    session_id: '',
  }
}

export function useRound(): RoundValue {
  const v = useContext(RoundCtx)
  if (!v) throw new Error('useRound 必须在 RoundProvider 内使用')
  return v
}

export { STAGE_ORDER }
