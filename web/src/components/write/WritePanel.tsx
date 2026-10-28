/* 写 SQL 面板：人手工写一条写语句，走「预览影响面 → 勾选确认（破坏性还要手输表名）→ 落刀」。
   与只读主链路的差别：这里人工确认的对象是这份**执行计划**本身（P1 不让 LLM 生成写语句）。

   三条不能省的规矩：
   1) 没有预览就没有确认按钮 —— 数字（影响多少行、备份到哪）必须先摊开；
   2) 预览之后又改了 SQL / 换了库或档位，那份预览立刻作废，得重新预览；
   3) 回滚 SQL 只是给人看的指引，界面不代跑（自动回滚会掩盖真正的失败原因）。 */
import { useMemo, useState } from 'react'
import { writeApi } from '../../api'
import type { WriteAuditItem, WriteExecuteResult, WritePlan } from '../../api/types'
import { errMessage, useAsync } from '../../hooks/useAsync'
import { useApp } from '../../state/AppProvider'
import { useRound } from '../../session/RoundProvider'
import { SqlBlock, SqlEditor } from '../CodeBlock'
import { formatTime } from '../HistoryList'
import { AsyncView, Chip, EmptyState, ErrorNote } from '../Primitives'
import {
  AUDIT_STATUS_LABEL, AUDIT_STATUS_TONE, TIER_LABEL, backupPattern,
  executeReady, gateNotice, isStalePlan, noRollbackNote, type PreviewedRef,
} from '../../lib/writeGate'

export function WritePanel() {
  const app = useApp()
  const { state } = useRound()
  const [sql, setSql] = useState('')
  const [dbOverride, setDbOverride] = useState('')
  const [plan, setPlan] = useState<WritePlan | null>(null)
  const [previewed, setPreviewed] = useState<PreviewedRef | null>(null)
  const [ack, setAck] = useState(false)
  const [phrase, setPhrase] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [result, setResult] = useState<WriteExecuteResult | null>(null)
  const [showAudit, setShowAudit] = useState(false)

  /* 目标库：手填 > 提问上下文选的库 > 数据源当前库 */
  const targetDb = (dbOverride.trim() || app.selection.db || app.activeDs?.database || '').trim()

  const audit = useAsync(async () => (
    showAudit ? writeApi.audit({ limit: 50, ds_id: app.activeDs?.id }) : [] as WriteAuditItem[]
  ), [showAudit, app.activeDs?.id])

  const writesEnabled = app.health.data?.writes_enabled ?? null
  const notice = gateNotice({
    ds: app.activeDs,
    requested: app.writeTier,
    ceiling: app.writeCeiling,
    writesEnabled,
    maxRows: app.health.data?.write_max_affected_rows,
  })
  const stale = isStalePlan(previewed, { sql: sql.trim(), db: targetDb, tier: app.writeTier })
  const ready = executeReady(plan, ack, phrase)
  const canRun = Boolean(plan?.passed) && !stale && ready.ok && !busy
  /* 按钮点不动时，把原因写在旁边：只把按钮置灰、不解释，人只会以为功能坏了 */
  const blockers = !plan ? [] : stale ? ['SQL／目标库／档位已改动，请重新预览'] : ready.blockers

  function body() {
    return {
      sql: sql.trim(),
      ds_id: app.activeDs?.id ?? null,
      db: targetDb,
      tier: app.writeTier,
      session_id: state.sessionId || '',
    }
  }

  async function preview() {
    setBusy(true); setErr(''); setResult(null); setPlan(null); setPreviewed(null); setAck(false)
    try {
      const p = await writeApi.prepare(body())
      setPlan(p)
      setPreviewed({ sql: p.final_sql ?? sql.trim(), db: targetDb, tier: p.requested_tier })
    } catch (e: unknown) {
      setErr(errMessage(e))         // 403 的正文就是拦人的那条规则，原文摊开
    } finally {
      setBusy(false)
    }
  }

  async function execute() {
    setBusy(true); setErr('')
    try {
      const r = await writeApi.execute({ ...body(), confirmed: true, confirm_phrase: phrase })
      setResult(r)
      setPlan(null); setPreviewed(null); setAck(false); setPhrase('')
      if (showAudit) audit.reload()
    } catch (e: unknown) {
      setErr(errMessage(e))
      if (showAudit) audit.reload()   // 被拒/失败都会落审计，列表跟着刷新才看得见
    } finally {
      setBusy(false)
    }
  }

  const targets = useMemo(() => (plan?.targets ?? []), [plan])

  return (
    <div className="wp">
      <div className="wp-bar">
        <label className="wp-db">
          <span>目标库</span>
          <input value={dbOverride} placeholder={targetDb || '未选定'}
                 disabled={busy} maxLength={64}
                 onChange={(e) => setDbOverride(e.target.value)} />
        </label>
        <Chip tone={app.writeEffective === 'read' ? 'neutral' : 'warn'}>
          档位：{TIER_LABEL[app.writeEffective]}
        </Chip>
        {/* 总闸关着时生效档位也是只读，但压低它的不是源上限，别把原因说错 */}
        {writesEnabled !== false && app.writeTier !== app.writeEffective && (
          <Chip tone="warn">请求「{TIER_LABEL[app.writeTier]}」被源上限压低</Chip>
        )}
        <button className="btn-ghost btn-xs wp-audit-toggle"
                onClick={() => setShowAudit((v) => !v)}>
          {showAudit ? '收起写入记录' : '写入记录'}
        </button>
      </div>

      <p className={`note wp-notice is-${notice.tone}`}>{notice.text}</p>

      {showAudit && (
        <div className="wp-audit">
          <div className="insight-block-head">
            <b>写入记录（最近 50 条，只记这个数据源）</b>
            <button className="btn-ghost btn-xs" disabled={audit.loading} onClick={audit.reload}>
              刷新
            </button>
          </div>
          <AsyncView loading={audit.loading && !audit.data} error={audit.error}
                     isEmpty={(audit.data ?? []).length === 0}
                     empty={<EmptyState title="还没有写入记录"
                                        hint="被拒绝的尝试也会记录在这里 —— 有人拿越权档位试探，这张表看得见。" />}>
            <ul className="wp-audit-list">
              {(audit.data ?? []).map((a) => (
                <AuditRow key={a.id} row={a} />
              ))}
            </ul>
          </AsyncView>
        </div>
      )}

      <SqlEditor value={sql} onChange={setSql} title="要执行的写语句"
                 hint="单条语句；UPDATE/DELETE 必须带 WHERE；跨库、多表 JOIN 写、INTO OUTFILE 都会被拦。" />

      <div className="wp-actions">
        <button className="btn-ghost btn-sm" disabled={busy || !sql.trim()}
                onClick={() => void preview()}>
          {busy && !plan ? '预览中…' : '预览影响面'}
        </button>
        <span className="result-sub">预览不改一个字节：它只跑安检和 SELECT COUNT(*)。</span>
      </div>

      {err && <ErrorNote>{err}</ErrorNote>}

      {plan && (
        <div className={`wp-plan ${stale ? 'is-stale' : ''}`}>
          <div className="insight-block-head">
            <b>执行计划 · {plan.statement_label}</b>
            {plan.destructive && <Chip tone="bad">不可逆</Chip>}
            {stale && <Chip tone="warn">已改动，需重新预览</Chip>}
          </div>
          <dl className="wp-rows">
            <div><dt>目标库</dt><dd><code>{targetDb || '—'}</code></dd></div>
            <div>
              <dt>涉及表</dt>
              <dd>
                {targets.length === 0 && '—'}
                {targets.map((t, i) => (
                  <span key={`${t.database}.${t.table}.${i}`} className="wp-target">
                    <code>{t.database}.{t.table}</code>
                    {t.is_main && <Chip tone="accent">主表</Chip>}
                  </span>
                ))}
              </dd>
            </div>
            <div>
              <dt>影响预估</dt>
              <dd>
                {plan.estimated_rows === null
                  ? <>{plan.needs_affected_rows ? '无法预估（预估失败就不执行）' : '这条语句不预估行数（建对象/改结构）'}</>
                  : <>约 <b>{plan.estimated_rows}</b> 行 · 单次上限 {plan.max_allowed_rows} 行</>}
              </dd>
            </div>
            <div>
              <dt>备份</dt>
              <dd>
                {plan.backup_table
                  ? <>执行前自动备份到形如 <code>{backupPattern(plan.backup_table)}</code>
                     的新表（表名带时间戳与随机位，执行时才生成）</>
                  : '不备份（这条语句没有可备份的行，或 WRITE_AUTO_BACKUP 已关）'}
              </dd>
            </div>
            <div>
              <dt>生效档位</dt>
              <dd>
                {TIER_LABEL[plan.effective_tier]}
                {plan.requested_tier !== plan.effective_tier
                  && <>（请求 {TIER_LABEL[plan.requested_tier]}，按数据源上限压低）</>}
              </dd>
            </div>
          </dl>

          <details className="wp-detail">
            <summary>看它是怎么算出来的（预估 SQL / 备份 SQL）</summary>
            {plan.estimate_sql && <SqlBlock sql={plan.estimate_sql} title="预估用这条 SELECT" />}
            {plan.backup_sql && <SqlBlock sql={plan.backup_sql} title="备份用这条 CREATE TABLE … AS SELECT" />}
            {!plan.estimate_sql && !plan.backup_sql && (
              <p className="note">这条语句既不需要预估也不需要备份（例如 CREATE TABLE）。</p>
            )}
          </details>

          <SqlBlock sql={plan.final_sql ?? sql} title="将要执行的语句" />

          {plan.destructive ? (
            <label className="wp-phrase">
              <span>
                这是不可逆操作。请原样输入表名 <code>{plan.confirm_phrase}</code> 表示你知道它会被删：
              </span>
              <input value={phrase} onChange={(e) => setPhrase(e.target.value)}
                     disabled={busy} placeholder={plan.confirm_phrase}
                     aria-label={`输入表名 ${plan.confirm_phrase} 确认`} />
            </label>
          ) : (
            <p className="note wp-hint">
              这条不是删表级操作，勾选确认即可执行；UPDATE/DELETE 的受影响行会先备份。
            </p>
          )}

          <label className="wp-ack">
            <input type="checkbox" checked={ack} disabled={busy || stale}
                   onChange={(e) => setAck(e.target.checked)} />
            我已看清上面的影响面（目标表、约 {plan.estimated_rows ?? '—'} 行、备份表形如
            {' '}{plan.backup_table ? backupPattern(plan.backup_table) : '无'}），确认执行
          </label>

          {!canRun && blockers.length > 0 && (
            <ul className="wp-blockers">
              {blockers.map((b) => <li key={b}>{b}</li>)}
            </ul>
          )}

          <div className="modal-actions">
            <button className="btn-ghost btn-sm" disabled={busy}
                    onClick={() => { setPlan(null); setPreviewed(null); setAck(false); setPhrase('') }}>
              取消
            </button>
            <button className="btn-primary btn-sm" disabled={!canRun} onClick={() => void execute()}>
              {busy ? '执行中…' : '确认执行'}
            </button>
          </div>
        </div>
      )}

      {result && (
        <div className="wp-result">
          <div className="insight-block-head">
            <b>已执行 · {result.statement_label}</b>
            <Chip tone="ok">审计 #{result.audit_id}</Chip>
          </div>
          <p className="result-sub">
            影响 {result.affected_rows} 行 · 耗时 {result.cost_ms} ms
            {result.backup_table ? <> · 备份表 <code>{result.backup_table}</code></> : ' · 无备份表'}
            {result.backup_rows !== null && result.backup_rows !== undefined
              ? <>（{result.backup_rows} 行）</> : null}
          </p>
          {result.rollback_sql
            ? (
              <>
                <SqlBlock sql={result.rollback_sql} title="回滚指引（系统不代跑）" tone="warn" />
                <p className="note">要恢复就自己核对后执行这条；系统不替你自动回滚，避免把失败掩盖成"已恢复"。</p>
              </>
            )
            : <p className="note">{noRollbackNote(result.statement_kind)}</p>}
        </div>
      )}
    </div>
  )
}

function AuditRow({ row }: { row: WriteAuditItem }) {
  const tone = AUDIT_STATUS_TONE[row.status] ?? 'neutral'
  return (
    <li className={`wp-audit-row is-${row.status}`}>
      <div className="wp-audit-head">
        <Chip tone={tone}>{AUDIT_STATUS_LABEL[row.status] ?? row.status}</Chip>
        {/* 被拒的语句没走到分类那一步，kind 是空的：补个破折号只会读成"被拒绝—库名" */}
        {row.statement_kind && <b>{row.statement_kind}</b>}
        <code className="wp-audit-db">{row.target_db || '—'}</code>
        <span className="wp-audit-time">{formatTime(row.created_at)}</span>
      </div>
      <p className="wp-audit-sql">{row.sql}</p>
      <p className="wp-audit-meta">
        档位 {row.requested_tier}
        {row.effective_tier && row.effective_tier !== row.requested_tier
          ? ` → ${row.effective_tier}` : ''}
        {' · '}预估 {row.estimated_rows ?? '—'} / 实际 {row.affected_rows ?? '—'} 行
        {row.backup_table ? <> · 备份 <code>{row.backup_table}</code></> : null}
        {row.destructive ? ' · 不可逆' : ''}
        {row.cost_ms ? ` · ${row.cost_ms} ms` : ''}
      </p>
      {row.error && <p className="wp-audit-err">{row.error}</p>}
    </li>
  )
}
