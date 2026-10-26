/* 单阶段聚焦视图：主区一次只展开一个阶段，其余阶段收起为可切换的列表项。

   一块阶段卡片 = 头部（序号/名称/状态/尝试切换/编辑与重跑）
                 + 该次尝试的过程消息（思考 / 工具调用 / 确认 / 输出）
                 + 阶段产出与编辑区（见 StageBodies）
                 + 底部操作（阶段主动作 + 上一步/下一步）。

   阶段被重跑过会留下多次尝试，用「‹ 第 n/m 次 ›」切换回看任意一次，
   这就是同一对话内的分支：分叉发生在阶段内部，而不是复制整段对话。 */
import { useRound } from '../../session/RoundProvider'
import {
  STAGE_META, STAGE_ORDER, intentEdited, stageStatus,
  type StageEvent, type StageKey, type StageStatus,
} from '../../session/machine'
import { StageBody } from './StageBodies'
import { EventIcon, RefreshIcon } from '../icons'
import { Chip, Spinner } from '../Primitives'

const STATUS_TEXT: Record<StageStatus, string> = {
  idle: '未开始', running: '进行中', ok: '已完成',
  await: '等你确认', failed: '失败', stale: '需重跑',
}

/** 可编辑的阶段：其余阶段产出由后端决定，改了也无处回传 */
const EDITABLE: StageKey[] = ['parse', 'confirm', 'analysis']

export function StagePanel() {
  const { state, goto, startEdit, rerun, confirmIntent, confirmSql, viewAttempt } = useRound()
  const focus = state.focus
  const stage = state.stages[focus]
  const status = stageStatus(state, focus)
  const idx = STAGE_ORDER.indexOf(focus)
  const attempt = stage.attempts[stage.view] ?? null
  const editing = state.editing === focus
  const busy = state.busy

  /* 确认阶段：SQL 被改过就带草稿执行，后端会把这条回写进会话。
     注意 sqlDraft 为 null 表示"从没编辑过"，不能当成空串去比 —— 那样一进确认
     阶段就会被误判成改过，按钮文案与执行分支都会错。 */
  const sqlChanged = state.sqlDraft !== null
    && state.sqlDraft.trim() !== (state.sql?.final_sql ?? '').trim()
  const customSql = sqlChanged ? (state.sqlDraft ?? '') : undefined

  return (
    <div className="stage-flow">
      <section className={`stage-card is-${status} animate-in`}>
        <header className="stage-card-head">
          <span className="stage-seq">{idx + 1}</span>
          <div className="stage-title">
            <b>{STAGE_META[focus].label}</b>
            <span>{STAGE_META[focus].desc}</span>
          </div>
          <span className={`badge badge-${status}`}>{STATUS_TEXT[status]}</span>

          <div className="stage-tools">
            {stage.attempts.length > 1 && (
              <span className="attempt-switch">
                <button className="btn-ghost btn-xs" disabled={stage.view <= 0}
                        onClick={() => viewAttempt(focus, stage.view - 1)} aria-label="上一次尝试">‹</button>
                <em>第 {stage.view + 1}/{stage.attempts.length} 次</em>
                <button className="btn-ghost btn-xs" disabled={stage.view >= stage.attempts.length - 1}
                        onClick={() => viewAttempt(focus, stage.view + 1)} aria-label="下一次尝试">›</button>
              </span>
            )}
            {EDITABLE.includes(focus) && (
              <button className={`btn-ghost btn-sm ${editing ? 'on' : ''}`}
                      onClick={() => startEdit(editing ? null : focus)} disabled={busy}>
                {editing ? '完成编辑' : '编辑'}
              </button>
            )}
            <button className="btn-ghost btn-sm" onClick={() => void rerun(focus)} disabled={busy}
                    title="重新执行本阶段（会作废其后各阶段的结果）">
              <RefreshIcon />重跑
            </button>
          </div>
        </header>

        {attempt?.edited && <p className="stage-note">本次尝试使用了手工编辑的输入</p>}
        {attempt && attempt.events.length > 0 && <EventList events={attempt.events} />}
        {attempt?.error && <p className="note note-bad">{attempt.error}</p>}

        <StageBody stage={focus} attempt={attempt} />

        <footer className="stage-card-foot">
          <div className="stage-actions">
            {focus === 'parse' && (
              <button className="btn-primary" disabled={busy} onClick={() => void confirmIntent()}>
                {intentEdited(state) ? '用修改后的意图生成 SQL' : '确认意图，生成 SQL'}
              </button>
            )}
            {focus === 'confirm' && (
              <button className="btn-primary" disabled={busy || !state.sql?.passed}
                      onClick={() => void confirmSql(customSql)}>
                {sqlChanged ? '确认执行（使用修改版 SQL）' : '确认执行'}
              </button>
            )}
            {focus === 'sql' && state.sql?.passed && (
              <button className="btn-primary" onClick={() => goto('confirm')}>去人工确认</button>
            )}
          </div>

          <div className="stage-nav">
            <button className="btn-ghost btn-sm" disabled={idx === 0}
                    onClick={() => goto(STAGE_ORDER[idx - 1])}>← 上一阶段</button>
            <button className="btn-ghost btn-sm" disabled={idx === STAGE_ORDER.length - 1}
                    onClick={() => goto(STAGE_ORDER[idx + 1])}>下一阶段 →</button>
          </div>
        </footer>
      </section>

      {/* 其余阶段：收起为一行一项，点击即切换聚焦 */}
      <section className="stage-others animate-in">
        <div className="stage-others-head">
          <b>其余阶段</b>
          <span>点击切换</span>
        </div>
        <ul>
          {STAGE_ORDER.filter((k) => k !== focus).map((k) => {
            const st = stageStatus(state, k)
            return (
              <li key={k}>
                <button onClick={() => goto(k)}>
                  <i className={`mini-dot is-${st}`} aria-hidden="true" />
                  <b>{STAGE_ORDER.indexOf(k) + 1}. {STAGE_META[k].label}</b>
                  <em>{STATUS_TEXT[st]}</em>
                  {state.stages[k].attempts.length > 1 &&
                    <span className="branch-flag" title="该阶段被重跑过">⑂ {state.stages[k].attempts.length}</span>}
                </button>
              </li>
            )
          })}
        </ul>
      </section>
    </div>
  )
}

const EVENT_STATE: Record<string, string> = {
  running: '进行中', ok: '已完成', await: '等你确认', failed: '失败',
}

function EventList({ events }: { events: StageEvent[] }) {
  return (
    <ul className="ev-list stagger">
      {events.map((e) => (
        <li key={e.key} className={`ev is-${e.status}`}>
          <span className="ev-icon"><EventIcon kind={e.kind} /></span>
          <div className="ev-main">
            <div className="ev-head">
              <b>{e.title}</b>
              <span className="ev-state">
                {e.status === 'running' && <Spinner />}
                {EVENT_STATE[e.status]}
              </span>
            </div>
            {e.detail && e.detail.length > 0 && (
              <div className="ev-detail">
                {e.detail.map((d, i) => (
                  <span key={i}>
                    {e.kind === 'tool' || e.kind === 'confirm'
                      ? <Chip tone="mono">{d}</Chip>
                      : d}
                  </span>
                ))}
              </div>
            )}
          </div>
        </li>
      ))}
    </ul>
  )
}
