/* 历史回看：只读渲染某一轮的完整记录（问题 / 意图 / SQL / 结果 / 洞察 / 图表）。
   数据源是后端的 session 记录，字段与当前轮完全一致，因此复用同一批展示组件。 */
import { useState } from 'react'
import { roundsApi } from '../api'
import type { ChartKind } from '../api/types'
import { useAsync } from '../hooks/useAsync'
import { useApp } from '../state/AppProvider'
import { AsyncView, Chip, ErrorNote } from './Primitives'
import { SqlBlock } from './CodeBlock'
import { ResultView } from './ResultView'
import { ConfirmDialog } from './ConfirmDialog'
import { formatTime } from './HistoryList'

export function ReplayView({ sessionId }: { sessionId: string }) {
  const app = useApp()
  const data = useAsync(() => roundsApi.get(sessionId), [sessionId])
  /* 回看场景的图表参数只在本地保留，不进会话状态 */
  const [prefs, setPrefs] = useState<{ chart: ChartKind; rows: number }>({ chart: 'bar', rows: 100 })
  const [err, setErr] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [pending, setPending] = useState(false)

  const session = data.data

  async function remove() {
    setPending(true)
    try {
      await roundsApi.remove(sessionId)
      app.setReplayId(null)
      app.rounds.reload()
      setConfirming(false)
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : '删除失败')
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="replay animate-in">
      <div className="replay-head">
        <div>
          <b>回看这一轮</b>
          <span className="replay-sub">
            {session ? formatTime(session.created_at) : ''}
            {' · '}只读记录
          </span>
        </div>
        <div className="replay-actions">
          <button className="btn-ghost btn-sm" onClick={() => app.setReplayId(null)}>
            返回当前对话
          </button>
          <button className="btn-ghost btn-sm btn-danger" onClick={() => setConfirming(true)}>
            删除本轮
          </button>
        </div>
      </div>

      {err && <ErrorNote>{err}</ErrorNote>}

      <AsyncView loading={data.loading} error={data.error}>
        {session && (
          <div className="replay-body">
            <div className="replay-q">
              <span className="replay-label">问题</span>
              <p>{session.user_question}</p>
            </div>

            <div className="replay-meta">
              <Chip tone="accent">{session.intent || '未记录意图'}</Chip>
              <Chip tone="mono">{session.target_db || '—'}</Chip>
              <Chip tone={session.intent_confirmed ? 'ok' : 'warn'}>
                {session.intent_confirmed ? '意图已确认' : '意图未确认'}
              </Chip>
              <Chip tone={session.sql_confirmed ? 'ok' : 'warn'}>
                {session.sql_confirmed ? 'SQL 已确认' : 'SQL 未确认'}
              </Chip>
            </div>

            {session.sql && <SqlBlock sql={session.sql} title="本轮执行的 SQL" />}

            {session.query_result && session.analysis_result
              ? <ResultView query={session.query_result} analysis={session.analysis_result}
                            sql={session.sql} prefs={prefs} onPrefsChange={(p) => setPrefs((v) => ({ ...v, ...p }))} />
              : <p className="panel-hint">
                  这一轮没有产出结果（中途取消或被安全规则拦截）。
                </p>}
          </div>
        )}
      </AsyncView>

      <ConfirmDialog
        open={confirming}
        title="确认删除这一轮记录？"
        target={session?.user_question}
        hint="删除后不可恢复，包含这一轮的问题、SQL 与查询结果。"
        pending={pending}
        onConfirm={() => void remove()}
        onCancel={() => setConfirming(false)}
      />
    </div>
  )
}
