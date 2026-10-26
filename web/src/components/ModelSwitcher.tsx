/* 模型切换：把"现在用哪个模型"放到提问区/顶栏上直接换，不用绕进供应商面板。
   复用 ProviderPanel 已经跑通的激活接口（后端保证全局唯一 is_active），
   这里只是把入口挪到手边，行为必须与面板一致：点一下就换，不做二次确认。

   三条刻意的设计：
   1) 只列「已启用供应商」下的模型 —— 切到禁用的供应商等于切到一个调不通的模型；
   2) 菜单状态留在本组件：AppProvider 的 value 每次渲染都新建，
      放进全局会让整棵树随提问输入逐字符重渲染；
   3) 位置随入口变：提问区在屏幕底部，菜单只能朝上弹；顶栏朝下。 */
import { useEffect, useRef, useState } from 'react'
import { providersApi } from '../api'
import { errMessage } from '../hooks/useAsync'
import { useApp } from '../state/AppProvider'
import { CheckIcon, ModelIcon } from './icons'
import { Chip } from './Primitives'

export function ModelSwitcher({ tone = 'btn' }: { tone?: 'btn' | 'chip' }) {
  const app = useApp()
  const wrap = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState<number | null>(null)   // 正在切换的 model_id
  const [err, setErr] = useState('')

  /* 点外面 / Esc 都要能收起：菜单是浮层，没有遮罩，收起不了就会挡住下面的按钮 */
  useEffect(() => {
    if (!open) return
    function onDown(e: MouseEvent) {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const usable = (app.providers.data ?? []).filter((p) => p.enabled && p.models.length > 0)

  async function pick(providerId: number, modelId: number) {
    setPending(modelId)
    setErr('')
    try {
      await providersApi.activateModel(providerId, modelId)
      app.providers.reload()
      setOpen(false)
    } catch (e: unknown) {
      setErr(errMessage(e))          // 失败时菜单留着，让用户看得见原因
    } finally {
      setPending(null)
    }
  }

  /* 未接入任何模型时回落到 healthz 报的配置（环境变量模型或"规则兜底"）：
     顶栏原来就显示这个，切换器不能把它读丢 —— 显示"未接入模型"会让人以为坏了。 */
  const envLlm = app.health.data?.llm ?? ''
  const isRule = envLlm.includes('规则兜底')
  const shown = app.activeModel?.model_name
    ?? (envLlm ? (isRule ? '规则兜底' : envLlm) : '未接入模型')

  return (
    <div className={`model-switch is-${tone}`} ref={wrap}>
      {tone === 'btn' ? (
        <button className={`btn-ghost btn-sm ${open ? 'on' : ''}`}
                onClick={() => { setErr(''); setOpen((v) => !v) }}
                aria-haspopup="menu" aria-expanded={open}
                title="切换提问使用的模型（意图解析与结果解读都走它）">
          <ModelIcon />模型<em className="btn-tag">{shown}</em>
          <span className="ms-caret">▾</span>
        </button>
      ) : (
        <button className={`topbar-model ${open ? 'on' : ''}`}
                onClick={() => { setErr(''); setOpen((v) => !v) }}
                aria-haspopup="menu" aria-expanded={open}
                title={`当前模型 ${shown}，点击切换`}>
          {/* 复用 Chip：顶栏原来的外观就是它，换控件不该把配色换掉 */}
          <Chip tone={app.activeModel || !isRule ? 'accent' : 'warn'}>
            <ModelIcon />{shown}
          </Chip>
          <span className="ms-caret">▾</span>
        </button>
      )}

      {open && (
        <div className="ms-menu" role="menu">
          <div className="ms-head">切换模型</div>
          {usable.length === 0 && (
            <p className="ms-empty">还没有接入可用模型：先到供应商面板接入一家，再回来切换。</p>
          )}
          {usable.map((p) => (
            <div key={p.id} className="ms-group">
              <div className="ms-provider">
                {p.name}
                {!p.has_key && <em className="ms-warn">无密钥</em>}
              </div>
              {p.models.map((m) => {
                const on = m.is_active
                return (
                  <button key={m.id} role="menuitemradio" aria-checked={on}
                          className={`ms-item ${on ? 'on' : ''}`}
                          disabled={pending !== null}
                          onClick={() => void pick(p.id, m.id)}>
                    <span className="ms-check">{on && <CheckIcon />}</span>
                    <code>{m.model_name}</code>
                    {pending === m.id && <em className="ms-busy">切换中…</em>}
                  </button>
                )
              })}
            </div>
          ))}
          {err && <p className="note note-bad ms-err">{err}</p>}
          <button className="ms-foot"
                  onClick={() => { setOpen(false); app.openDrawer('providers') }}>
            管理供应商 / 接入新模型…
          </button>
        </div>
      )}
    </div>
  )
}
