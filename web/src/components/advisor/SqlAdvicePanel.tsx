/* 阶段③的「SQL 优化建议」：分析用户（手工改过的）SQL，给出查询优化方案。

   触发策略（对应后端的分层）：
   - 规则引擎（narrate=false）：编辑停下后自动跑一次——即时、免费，不花 token；
   - AI 解读（narrate=true）：点按钮才调 LLM 把结论讲成业务影响，避免每次按键都花钱。

   不执行 SQL、不改写会话，纯静态剖析。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { agentApi } from '../../api'
import type { SqlAdviceResult } from '../../api/types'
import { errMessage } from '../../hooks/useAsync'
import { useApp } from '../../state/AppProvider'
import { useRound } from '../../session/RoundProvider'
import { Chip, ErrorNote } from '../Primitives'
import { AdviceList } from './AdviceItem'
import { ModelIcon, RefreshIcon } from '../icons'

interface Props {
  sql: string
}

export function SqlAdvicePanel({ sql }: Props) {
  const app = useApp()
  const { state } = useRound()
  const [data, setData] = useState<SqlAdviceResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [narrating, setNarrating] = useState(false)
  const [error, setError] = useState('')
  /* 只对"当前这条 SQL"的建议负责：SQL 一变，旧建议立即作废（不留着误导人） */
  const analyzedFor = useRef('')

  const run = useCallback(async (narrate: boolean) => {
    if (!sql.trim()) return
    if (narrate) setNarrating(true)
    else setLoading(true)
    setError('')
    try {
      const r = await agentApi.sqlAdvice({
        sql,
        ds_id: app.activeDs?.id ?? null,
        session_id: state.sessionId || undefined,
        narrate,
      })
      setData(r)
      analyzedFor.current = sql
    } catch (e: unknown) {
      setError(errMessage(e))
    } finally {
      setLoading(false)
      setNarrating(false)
    }
  }, [sql, app.activeDs, state.sessionId])

  /* 编辑停下后自动跑一次规则引擎（debounce 700ms：连续打字不反复请求） */
  useEffect(() => {
    if (!sql.trim()) return
    if (analyzedFor.current === sql) return
    const t = setTimeout(() => { void run(false) }, 700)
    return () => clearTimeout(t)
  }, [sql, run])

  if (!sql.trim()) return null

  const stale = data !== null && analyzedFor.current !== sql

  return (
    <div className="advice-panel">
      <div className="advice-toolbar">
        <span className="advice-toolbar-title">
          <ModelIcon />优化建议
          {stale && <Chip tone="warn">SQL 已改动，建议过期</Chip>}
        </span>
        <span className="advice-toolbar-actions">
          <button className="btn-ghost btn-xs" disabled={loading}
                  onClick={() => void run(false)} title="重新跑规则引擎（即时、不花 token）">
            <RefreshIcon />{loading ? '分析中…' : '重新分析'}
          </button>
          <button className="btn-ghost btn-xs" disabled={narrating || loading}
                  onClick={() => void run(true)}
                  title="调大模型把结论讲成业务影响（按需触发）">
            {narrating ? '解读中…' : 'AI 解读'}
          </button>
        </span>
      </div>

      {error && <ErrorNote>{error}</ErrorNote>}

      {data && (
        <>
          {data.summary && <p className="advice-summary">{data.summary}</p>}
          {!data.readable && (
            <p className="note note-bad">
              这条 SQL 不是只读 SELECT，无法执行；优化建议仍可参考，但请先改回只读语句。
            </p>
          )}
          {data.parse_error && <p className="note note-warn">{data.parse_error}</p>}

          {data.facts.length > 0 && (
            <ul className="fact-list">
              {data.facts.map((f) => (
                <li key={f.table}>
                  <code>{f.table}</code>
                  <span className="fact-rows">{f.row_estimate?.toLocaleString() ?? '—'} 行</span>
                  <span className="fact-cols">{f.column_count} 列</span>
                  <Chip tone={f.indexes.length ? 'ok' : 'bad'}>
                    {f.indexes.length
                      ? f.indexes.map((i) => `${i.name}(${(i.columns || []).join(', ')})`).join('；')
                      : '无索引'}
                  </Chip>
                </li>
              ))}
            </ul>
          )}

          <AdviceList advices={data.advices}
                      ctx={{ sourceKind: 'sql_advice', dsId: app.activeDs?.id }} />
          {data.llm_used && (
            <p className="advice-foot">
              以上为大模型（{data.model_label}）基于规则结论的解读；规则部分不依赖模型。
            </p>
          )}
        </>
      )}
    </div>
  )
}
