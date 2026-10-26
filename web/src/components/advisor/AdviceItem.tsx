/* 优化建议条目：两个面板（数据源洞察 / SQL 建议）共用。

   Data Dense 落地：severity 用颜色编码（high=红、medium=黄、low=蓝、info=灰），
   建议标题加粗、依据与做法分两级缩进——扫一眼标题就能决定要不要点开。
   依据（evidence）等宽显示：它是 information_schema / AST 的硬事实，
   和"猜测"区分开，用户才敢信。

   右侧「加入清单」把这条建议变成待办：建议本身会随数据变化重算，
   所以同时把 db/table/code/evidence 存进 source_ref 快照，任务长期可解释。 */
import { useState } from 'react'
import type { Advice, TaskSourceKind } from '../../api/types'
import { useApp } from '../../state/AppProvider'
import { Chip } from '../Primitives'

const SEV: Record<string, { tone: 'bad' | 'warn' | 'info' | 'neutral'; label: string }> = {
  high: { tone: 'bad', label: '影响大' },
  medium: { tone: 'warn', label: '建议改' },
  low: { tone: 'info', label: '可优化' },
  info: { tone: 'neutral', label: '提示' },
}

/** 这条建议出自哪里：加入任务清单时带上，面板能区分来源、任务能记住出处 */
export interface AdviceCtx {
  sourceKind?: TaskSourceKind
  db?: string
  table?: string
  dsId?: number
}

export function AdviceItem({ advice, index, ctx }: {
  advice: Advice
  index: number
  ctx?: AdviceCtx
}) {
  const app = useApp()
  const sev = SEV[advice.severity] ?? SEV.info
  const [added, setAdded] = useState(false)
  const [busy, setBusy] = useState(false)

  async function addToList() {
    setBusy(true)
    try {
      await app.addTask({
        title: advice.title,
        note: advice.suggestion || advice.detail,
        source_kind: ctx?.sourceKind ?? 'advice',
        ds_id: ctx?.dsId ?? app.activeDs?.id ?? 0,
        source_ref: {
          code: advice.code, severity: advice.severity, evidence: advice.evidence,
          db: ctx?.db ?? '', table: ctx?.table ?? '',
        },
      })
      setAdded(true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className={`advice is-${advice.severity}`} style={{ ['--i' as string]: index }}>
      <div className="advice-head">
        <Chip tone={sev.tone}>{sev.label}</Chip>
        <b>{advice.title}</b>
        <button className={`advice-add ${added ? 'is-done' : ''}`}
                disabled={added || busy} onClick={() => void addToList()}
                title="加入当前项目的任务清单：可调序、可核对后统一执行">
          {added ? '已在清单' : busy ? '加入中…' : '加入清单'}
        </button>
      </div>
      {advice.detail && <p className="advice-detail">{advice.detail}</p>}
      {advice.suggestion && (
        <p className="advice-how">
          <span className="advice-how-tag">怎么做</span>
          {advice.suggestion}
        </p>
      )}
      {advice.evidence && (
        <p className="advice-evidence">
          <span className="advice-evidence-tag">依据</span>
          <code>{advice.evidence}</code>
        </p>
      )}
    </li>
  )
}

export function AdviceList({ advices, ctx }: { advices: Advice[]; ctx?: AdviceCtx }) {
  if (advices.length === 0) {
    return <p className="panel-hint">未发现可优化点。</p>
  }
  return <ul className="advice-list stagger">{advices.map((a, i) => (
    <AdviceItem key={`${a.code}-${i}`} advice={a} index={i} ctx={ctx} />
  ))}</ul>
}
