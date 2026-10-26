/* 各阶段的产出区：既展示结果，也提供该阶段的编辑入口。

   可编辑的阶段与其依据：
     parse   意图文本 / 目标库 / 目标表 / 目标字段  → 整张卡片回传给 generate_sql
     confirm SQL 语句                              → execute_sql 支持传自定义 SQL
     analysis 图表类型 / 明细行数                   → 纯前端，不重跑查询
   sql 与 execute 阶段不提供编辑（产出由后端决定，改了也没处回传）。 */
import { useRound } from '../../session/RoundProvider'
import type { StageAttempt, StageKey } from '../../session/machine'
import { SqlBlock, SqlEditor } from '../CodeBlock'
import { ResultView } from '../ResultView'
import { SourceInsightPanel } from '../advisor/SourceInsightPanel'
import { SqlAdvicePanel } from '../advisor/SqlAdvicePanel'
import { Chip, CountUp, EmptyState, Kpi } from '../Primitives'

interface Props { stage: StageKey; attempt: StageAttempt | null }

export function StageBody({ stage, attempt }: Props) {
  switch (stage) {
    case 'parse': return <ParseBody attempt={attempt} />
    case 'sql': return <SqlBody attempt={attempt} />
    case 'confirm': return <ConfirmBody attempt={attempt} />
    case 'execute': return <ExecuteBody attempt={attempt} />
    case 'analysis': return <AnalysisBody attempt={attempt} />
    default: return null
  }
}

const splitList = (v: string) =>
  v.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean)

/* ---------------------------------------------------------------- ① 解析意图 */
function ParseBody({ attempt }: { attempt: StageAttempt | null }) {
  const { state, startEdit, setIntentDraft } = useRound()
  const card = attempt?.intent ?? state.intent
  if (!card) return <EmptyState title="尚无意图卡片" hint="先提问，Agent 会解析出意图" />

  const editing = state.editing === 'parse'
  const draft = state.intentDraft

  return (
    <div className="stage-body">
      {editing ? (
        <div className="intent-edit">
          <label className="field">
            <span>分析意图</span>
            <textarea rows={2} value={draft?.intent ?? card.intent}
                      onChange={(e) => setIntentDraft({ intent: e.target.value })} />
          </label>
          <div className="field-row">
            <label className="field">
              <span>目标库</span>
              <input value={draft?.target_db ?? card.target_db}
                     onChange={(e) => setIntentDraft({ target_db: e.target.value })} />
            </label>
            <label className="field">
              <span>目标表（逗号分隔）</span>
              <input value={(draft?.target_tables ?? card.target_tables).join(', ')}
                     onChange={(e) => setIntentDraft({ target_tables: splitList(e.target.value) })} />
            </label>
          </div>
          <label className="field">
            <span>目标字段（逗号分隔，可留空）</span>
            <input value={(draft?.target_fields ?? card.target_fields).join(', ')}
                   onChange={(e) => setIntentDraft({ target_fields: splitList(e.target.value) })} />
          </label>
          <p className="note">
            改完点「用修改后的意图重新生成 SQL」。也可以在输入区用「库表结构」勾选后重新提问。
          </p>
          <div className="form-actions">
            <button className="btn-ghost" onClick={() => startEdit(null)}>取消编辑</button>
          </div>
        </div>
      ) : (
        <div className="confirm-grid">
          <div className="confirm-row">
            <span className="confirm-key">分析意图</span>
            <span className="confirm-val">{card.intent}</span>
          </div>
          <div className="confirm-row">
            <span className="confirm-key">目标库</span>
            <span className="confirm-val mono">{card.target_db || '—'}</span>
          </div>
          <div className="confirm-row">
            <span className="confirm-key">目标表</span>
            <span className="confirm-val">
              {card.target_tables.length
                ? card.target_tables.map((t) => <Chip key={t} tone="mono">{t}</Chip>)
                : '自动识别'}
            </span>
          </div>
          {card.target_fields.length > 0 && (
            <div className="confirm-row">
              <span className="confirm-key">目标字段</span>
              <span className="confirm-val">
                {card.target_fields.map((f) => <Chip key={f} tone="mono">{f}</Chip>)}
              </span>
            </div>
          )}
          <div className="confirm-row">
            <span className="confirm-key">风险等级</span>
            <span className="confirm-val">
              <Chip tone={card.risk_level === 'medium' ? 'warn' : 'ok'}>
                {card.risk_level === 'medium' ? '中' : '低'}
              </Chip>
            </span>
          </div>
        </div>
      )}

      {card.sql_preview.trim() && (
        <SqlBlock sql={card.sql_preview} title="意图预览 SQL（未校验，仅供参考）" />
      )}
      {card.hint && <p className="note">{card.hint}</p>}

      {/* 数据源洞察：智能体对所选数据源的系统性描述与分析（含查询优化建议） */}
      <SourceInsightPanel />
    </div>
  )
}

/* ---------------------------------------------------------------- ② 生成 SQL */
function SqlBody({ attempt }: { attempt: StageAttempt | null }) {
  const result = attempt?.sql ?? null
  if (!result) return <EmptyState title="尚无 SQL" hint="确认意图后由 Agent 生成" />

  if (!result.passed) {
    return (
      <div className="stage-body">
        <p className="note note-bad">{result.reason || '安全规则拦截，未生成可执行 SQL'}</p>
        <p className="note">请退回「解析意图」阶段修改问题后重新生成。</p>
      </div>
    )
  }

  return (
    <div className="stage-body">
      <SqlBlock sql={result.final_sql || ''} title="生成的 SQL（已通过安全校验）" />
      <p className="note">
        后端已自动追加 LIMIT；下一步进入「人工确认」，你确认后才会真正访问数据库。
      </p>
    </div>
  )
}

/* ---------------------------------------------------------------- ③ 人工确认 */
function ConfirmBody({ attempt }: { attempt: StageAttempt | null }) {
  const { state, startEdit, setSqlDraft } = useRound()
  const original = state.sql?.final_sql ?? ''
  const editing = state.editing === 'confirm'
  const draft = state.sqlDraft ?? original
  const changed = draft.trim() !== original.trim()

  return (
    <div className="stage-body">
      {editing ? (
        <>
          <SqlEditor value={draft} onChange={setSqlDraft} title="修改即将执行的 SQL"
                     hint="只允许只读 SELECT；执行前后端会再做一次安全校验，非只读语句会被拒绝。" />
          {/* 改完立刻能看到这条 SQL 的问题在哪：规则引擎即时出结论，
              AI 解读按需触发（避免每次按键都调模型）。 */}
          <SqlAdvicePanel sql={draft} />
          <div className="form-actions">
            <button className="btn-ghost" onClick={() => { setSqlDraft(original); startEdit(null) }}>
              放弃修改
            </button>
            <button className="btn-ghost" onClick={() => setSqlDraft(original)}
                    disabled={!changed}>还原为原始 SQL</button>
          </div>
        </>
      ) : (
        <>
          {changed
            ? <SqlBlock sql={draft} title="待执行 SQL（已手工修改）" tone="warn"
                        note="与 Agent 生成的原始 SQL 不一致，执行后这条会回写到会话以便溯源。" />
            : <SqlBlock sql={original} title="待执行 SQL" />}
          <p className="note">
            确认后后端落 sql_confirmed；未确认时执行接口一律返回 409，无法绕过。
          </p>
        </>
      )}
      {attempt?.status === 'ok' && attempt.note && (
        <p className="note note-warn">{attempt.note}</p>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- ④ 执行查询 */
function ExecuteBody({ attempt }: { attempt: StageAttempt | null }) {
  const query = attempt?.query ?? null
  if (!query) return <EmptyState title="尚无查询结果" hint="在「人工确认」阶段点确认后执行" />

  return (
    <div className="stage-body">
      <div className="kpi-row">
        <Kpi label="返回行数" value={<CountUp value={query.row_count} />}
             foot={query.truncated ? '已触达上限' : '完整结果'}
             tone={query.truncated ? 'bad' : 'ok'} />
        <Kpi label="字段数" value={<CountUp value={query.columns.length} />} />
        <Kpi label="查询耗时" value={<><CountUp value={query.cost_ms} /><small>ms</small></>} />
        <Kpi label="执行状态" value={<span className="badge badge-ok">已完成</span>} />
      </div>
      <div className="confirm-row">
        <span className="confirm-key">字段</span>
        <span className="confirm-val">
          {query.columns.map((c) => <Chip key={c} tone="mono">{c}</Chip>)}
        </span>
      </div>
      {query.truncated && (
        <p className="note note-warn">结果触达行数上限已截断，可追加条件缩小范围。</p>
      )}
      <p className="note">明细与分析结论在下一步「分析输出」。</p>
    </div>
  )
}

/* ---------------------------------------------------------------- ⑤ 分析输出 */
function AnalysisBody({ attempt }: { attempt: StageAttempt | null }) {
  const { state, setPrefs } = useRound()
  const analysis = attempt?.analysis ?? state.analysis
  const query = attempt?.query ?? state.query
  if (!analysis || !query) {
    return <EmptyState title="尚无分析结论" hint="执行查询成功后自动产出" />
  }

  return (
    <div className="stage-body">
      <ResultView
        query={query}
        analysis={analysis}
        sql={state.sql?.final_sql ?? null}
        prefs={state.prefs}
        onPrefsChange={setPrefs}
      />
    </div>
  )
}
