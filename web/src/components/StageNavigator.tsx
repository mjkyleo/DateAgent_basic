/* 阶段导航：常驻页面右侧的浮动面板，浮于对话内容之上（fixed + z-index）。
   —— 五个阶段节点纵向排列，显示实时状态与尝试次数；点击即聚焦该阶段；
      整块可收起为一列小圆点，需要时再展开。

   收起态保留节点圆点（只是不带文字），这样"跑到第几步、卡在哪一步"
   在任何滚动位置都看得见。 */
import { useEffect, useState } from 'react'
import { useRound } from '../session/RoundProvider'
import { STAGE_META, STAGE_ORDER, stageStatus, type StageKey, type StageStatus } from '../session/machine'
import { ChevronIcon } from './icons'
import { Spinner } from './Primitives'

const STATUS_TEXT: Record<StageStatus, string> = {
  idle: '未开始', running: '进行中', ok: '已完成',
  await: '等你确认', failed: '失败', stale: '需重跑',
}

export function StageNavigator() {
  const { state, goto } = useRound()
  /* 窄屏默认收起：208px 的浮动面板在手机上会盖住近半屏内容，
     先以圆点条形态存在，需要时再展开（规则里"可显示与隐藏"的最低限度占用）。
     跨过 900px 时自动收起，但绝不自动展开 —— 那会和用户的手动操作打架。 */
  const [open, setOpen] = useState(() => window.innerWidth > 900)
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 900px)')
    const onChange = (e: MediaQueryListEvent) => { if (e.matches) setOpen(false) }
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  return (
    <aside className={`nav-float ${open ? '' : 'is-collapsed'}`} aria-label="阶段导航">
      <button className="nav-toggle" onClick={() => setOpen((v) => !v)}
              title={open ? '收起阶段面板' : '展开阶段面板'}
              aria-expanded={open}>
        <ChevronIcon dir={open ? 'right' : 'left'} />
      </button>

      {open ? (
        <>
          <div className="nav-head">
            <b>分析阶段</b>
            <span>{STAGE_ORDER.indexOf(state.focus) + 1}/{STAGE_ORDER.length}</span>
          </div>
          <ol className="nav-list">
            {STAGE_ORDER.map((key, i) => {
              const status = stageStatus(state, key)
              const stage = state.stages[key]
              const current = state.focus === key
              return (
                <li key={key}>
                  <button className={`nav-node is-${status} ${current ? 'is-current' : ''}`}
                          onClick={() => goto(key)}
                          aria-current={current ? 'step' : undefined}>
                    <span className="nav-idx">
                      {status === 'running' ? <Spinner /> : i + 1}
                    </span>
                    <span className="nav-text">
                      <b>{STAGE_META[key].label}</b>
                      <em>
                        {STATUS_TEXT[status]}
                        {stage.attempts.length > 1 && ` · 第 ${stage.attempts.length} 次`}
                      </em>
                    </span>
                    {stage.attempts.length > 1 && <span className="nav-branch" title="该阶段被重跑过">⑂</span>}
                  </button>
                </li>
              )
            })}
          </ol>
          <p className="nav-foot">点节点可回到该阶段查看或重跑</p>
        </>
      ) : (
        /* 收起态：一列状态圆点，点击任一圆点直接展开并跳转 */
        <ol className="nav-dots">
          {STAGE_ORDER.map((key, i) => {
            const status = stageStatus(state, key)
            return (
              <li key={key}>
                <button className={`nav-dot is-${status} ${state.focus === key ? 'is-current' : ''}`}
                        onClick={() => { goto(key); setOpen(true) }}
                        title={`${i + 1}. ${STAGE_META[key].label}（${STATUS_TEXT[status]}）`}>
                  {i + 1}
                </button>
              </li>
            )
          })}
        </ol>
      )}
    </aside>
  )
}

export type { StageKey }
