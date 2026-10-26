/* 底部弹出面板（数据源 / 库表结构）。
   从输入区上方升起、宽度与内容列对齐、内部滚动；点遮罩或 Esc 关闭。 */
import { useEffect, type ReactNode } from 'react'

interface Props {
  open: boolean
  title: string
  hint?: string
  onClose: () => void
  children: ReactNode
}

export function Sheet({ open, title, hint, onClose, children }: Props) {
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <>
      <div className="sheet-mask" onMouseDown={onClose} />
      <section className="sheet" role="dialog" aria-modal="true" aria-label={title}>
        <header className="sheet-head">
          <div>
            <b>{title}</b>
            {hint && <span className="sheet-hint">{hint}</span>}
          </div>
          <button className="btn-ghost btn-sm" onClick={onClose} aria-label="关闭">关闭</button>
        </header>
        <div className="sheet-body">{children}</div>
      </section>
    </>
  )
}
