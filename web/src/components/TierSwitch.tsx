/* 提问区的权限档位开关：人自己决定"这一轮给 Agent 多大权限"。
   档位只是**请求**，不是承诺：真正生效的值后端还要按数据源上限（allowed_ops）压一次，
   总闸（AGENT_ALLOW_WRITES）没开时更高档位一律写不下去 —— 所以开关上显示的是
   "生效值 + 一句为什么"，而不是让人以为点了就能写。 */
import { useEffect, useRef, useState } from 'react'
import { useApp } from '../state/AppProvider'
import { CheckIcon, ShieldIcon } from './icons'
import { gateNotice, tierChoices, TIER_LABEL } from '../lib/writeGate'
import type { PermTier } from '../api/types'

export function TierSwitch() {
  const app = useApp()
  const wrap = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)

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

  const writesEnabled = app.health.data?.writes_enabled ?? null
  const choices = tierChoices(app.writeCeiling, writesEnabled)
  const notice = gateNotice({
    ds: app.activeDs,
    requested: app.writeTier,
    ceiling: app.writeCeiling,
    writesEnabled,
    maxRows: app.health.data?.write_max_affected_rows,
  })
  /* 总闸关着时生效值也被压成只读，但原因不是源上限——那句"高于数据源上限"会说错话，
     这时候只留上面那条总闸提示。 */
  const clamped = writesEnabled !== false && app.writeTier !== app.writeEffective

  function pick(t: PermTier) {
    app.setWriteTier(t)
    setOpen(false)
  }

  return (
    <div className="tier-switch" ref={wrap}>
      <button className={`btn-ghost btn-sm ${open ? 'on' : ''}`}
              onClick={() => setOpen((v) => !v)}
              aria-haspopup="menu" aria-expanded={open}
              title="本次会话的写权限档位：只读 / 读写数据 / 结构变更">
        <ShieldIcon />权限<em className="btn-tag">{TIER_LABEL[app.writeEffective]}</em>
        {app.writeEffective !== 'read' && <span className={`tier-dot is-${notice.tone}`} />}
        <span className="ms-caret">▾</span>
      </button>

      {open && (
        <div className="ms-menu tier-menu" role="menu">
          <div className="ms-head">本次给 Agent 的权限</div>
          {choices.map((c) => {
            const on = app.writeTier === c.tier
            return (
              <button key={c.tier} role="menuitemradio" aria-checked={on}
                      className={`ms-item tier-item ${on ? 'on' : ''}`}
                      disabled={c.disabled}
                      title={c.disabled ? c.why : c.desc}
                      onClick={() => pick(c.tier)}>
                <span className="ms-check">{on && <CheckIcon />}</span>
                <span className="tier-text">
                  <b>{c.label}</b>
                  <em>{c.disabled ? c.why : c.desc}</em>
                </span>
              </button>
            )
          })}
          <p className={`note tier-notice is-${notice.tone}`}>
            {notice.text}
          </p>
          {clamped && (
            <p className="note tier-notice">
              你选的「{TIER_LABEL[app.writeTier]}」高于数据源上限，本次按
              「{TIER_LABEL[app.writeEffective]}」执行。
            </p>
          )}
          <button className="ms-foot" onClick={() => { setOpen(false); app.openDrawer('datasource') }}>
            去「数据源」把某个库标成可写…
          </button>
        </div>
      )}
    </div>
  )
}
