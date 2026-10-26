/* 最小原子组件：只做展示，不碰业务状态。 */
import { useEffect, useRef, useState, type ReactNode } from 'react'

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" aria-hidden="true" />
      {label && <span className="spinner-label">{label}</span>}
    </span>
  )
}

export type Tone = 'neutral' | 'accent' | 'ok' | 'warn' | 'bad' | 'mono' | 'info'

export function Chip({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`chip chip-${tone}`}>{children}</span>
}

/** KPI 指标卡：标签大写小字 + 数值等宽大字 + 可选脚注（Data Dense 统计卡） */
export function Kpi({ label, value, foot, tone }: {
  label: string
  value: ReactNode
  foot?: ReactNode
  tone?: 'ok' | 'bad' | 'accent'
}) {
  return (
    <div className="kpi">
      <span className="kpi-label" title={label}>{label}</span>
      <span className={`kpi-value ${tone ? `is-${tone}` : ''}`}>{value}</span>
      {foot !== undefined && <span className="kpi-foot">{foot}</span>}
    </div>
  )
}

const reduced = () =>
  typeof window !== 'undefined'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** 数字滚动：值变化时从旧值滚到新值（rAF + easeOutQuart，≈ ease-out-expo 手感）。
   用户要求"减少动效"时直接落终值，不做无谓动画。 */
export function CountUp({ value, duration = 420 }: { value: number; duration?: number }) {
  const [display, setDisplay] = useState(value)
  const fromRef = useRef(value)
  const rafRef = useRef(0)

  useEffect(() => {
    const from = fromRef.current
    if (from === value || reduced()) {
      fromRef.current = value
      setDisplay(value)
      return
    }
    const t0 = performance.now()
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / duration)
      const e = 1 - Math.pow(1 - p, 4)
      setDisplay(from + (value - from) * e)
      if (p < 1) rafRef.current = requestAnimationFrame(tick)
      else fromRef.current = value
    }
    rafRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafRef.current)
  }, [value, duration])

  /* 整数目标 → 动画过程也显示整数，避免出现 "3.7 行" 这种中间态 */
  const text = Number.isInteger(value)
    ? String(Math.round(display))
    : display.toFixed(1)
  return <span className="num-anim">{text}</span>
}

/** 骨架屏：块状内容加载时用微光占位，比转圈更接近最终布局 */
export function Skeleton({ w = '100%', h = 10, radius }: {
  w?: number | string; h?: number; radius?: number
}) {
  return (
    <span className="skeleton" aria-hidden="true"
          style={{ display: 'block', width: w, height: h, borderRadius: radius }} />
  )
}

export function EmptyState({ title, hint, children }: {
  title: string; hint?: string; children?: ReactNode
}) {
  return (
    <div className="empty-state">
      <b>{title}</b>
      {hint && <p>{hint}</p>}
      {children}
    </div>
  )
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return <p className="note note-bad" role="alert">{children}</p>
}

/** 面板加载/错误/空 三态的统一呈现。
    注意：是否为空由调用方用 isEmpty 判定 —— 组件无法判断 children 是否"没内容"。 */
export function AsyncView({ loading, error, isEmpty, empty, children }: {
  loading: boolean
  error: string | null
  isEmpty?: boolean
  empty?: ReactNode
  children: ReactNode
}) {
  if (loading) return <p className="panel-hint"><Spinner label="加载中…" /></p>
  if (error) return <ErrorNote>{error}</ErrorNote>
  if (isEmpty) return <>{empty ?? null}</>
  return <>{children}</>
}
