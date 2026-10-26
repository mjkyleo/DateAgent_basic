/* 删除确认模态框：替代浏览器原生 confirm()。
   原生弹窗有三个问题——样式与整体割裂、无法展示要删的具体对象、
   在部分浏览器里会被"不再询问"永久屏蔽。删除会话/模型都走这里。 */
import { useEffect, useRef, type ReactNode } from 'react'

interface Props {
  open: boolean
  title: string
  /** 要删除的对象描述（会以等宽字体显示，确保用户看清删的是哪条） */
  target?: ReactNode
  hint?: string
  confirmText?: string
  pending?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmDialog({ open, title, target, hint, confirmText = '删除',
                                pending = false, onConfirm, onCancel }: Props) {
  const okRef = useRef<HTMLButtonElement>(null)

  /* 打开即聚焦"删除"按钮：键盘用户不必先 Tab 一圈；
     Esc 关闭（不删），避免误触后找不到退路。 */
  useEffect(() => {
    if (!open) return
    okRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); onCancel() }
      if (e.key === 'Enter' && !pending) { e.preventDefault(); onConfirm() }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [open, pending, onConfirm, onCancel])

  if (!open) return null

  return (
    <div className="modal-mask" onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel() }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <b className="modal-title">{title}</b>
        {target && <div className="modal-target">{target}</div>}
        {hint && <p className="modal-hint">{hint}</p>}
        <div className="modal-actions">
          <button className="btn-ghost btn-sm" onClick={onCancel} disabled={pending}>
            取消
          </button>
          <button className="btn-danger btn-sm" ref={okRef} onClick={onConfirm}
                  disabled={pending}>
            {pending ? '处理中…' : confirmText}
          </button>
        </div>
      </div>
    </div>
  )
}
