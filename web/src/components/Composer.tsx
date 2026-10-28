/* 底部提问区：文本输入 + 上下文入口（数据源 / 库表结构 / 导入文件 / 任务清单）+ 提交。
   数据源与库表结构做成底部弹出面板，不再占用侧栏。 */
import { useRef, useState } from 'react'
import { useRound } from '../session/RoundProvider'
import { useApp } from '../state/AppProvider'
import { errMessage, useAction } from '../hooks/useAsync'
import { Chip } from './Primitives'
import { ModelSwitcher } from './ModelSwitcher'
import { TierSwitch } from './TierSwitch'
import { ChecklistIcon, DatabaseIcon, PencilIcon, TableIcon, UploadIcon } from './icons'

export function Composer() {
  const app = useApp()
  const { state, ask } = useRound()
  const fileRef = useRef<HTMLInputElement>(null)
  const [upErr, setUpErr] = useState('')
  const upload = useAction(app.importFile)

  const busy = state.busy
  const canSubmit = Boolean(app.activeDs) && !busy && app.draft.trim().length > 0

  function submit() {
    if (!canSubmit) return
    const q = app.draft
    void ask(q).catch((e: unknown) => {
      // ask 内部已落失败事件，这里只兜住"没数据源"这类前置错误
      if (!app.activeDs) setUpErr(errMessage(e))
    })
  }

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    e.target.value = ''            // 允许重复选择同一文件
    if (!f) return
    setUpErr('')
    await upload.run(f)
    if (upload.error) setUpErr(upload.error)
  }

  const ctx: string[] = []
  if (app.selection.db) ctx.push(app.selection.db)
  ctx.push(...app.selection.tables)
  if (app.selection.fields.length) ctx.push(`字段 ${app.selection.fields.length} 个`)

  /* 入口上的角标 = 当前项目未完成的任务数：从建议面板加了任务之后要能在别处看到它"收到了" */
  const pending = (app.tasks.data ?? []).filter((t) => t.status !== 'done').length

  return (
    <section className="composer">
      <textarea
        value={app.draft}
        onChange={(e) => app.setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
        }}
        rows={3}
        disabled={busy}
        placeholder="用自然语言提问，例如：统计 2025 年每月订单金额，看看趋势"
        aria-label="自然语言问题输入"
      />

      <div className="composer-foot">
        <div className="composer-left">
          <div className="composer-entries">
            <button className={`btn-ghost btn-sm ${app.drawer === 'datasource' ? 'on' : ''}`}
                    onClick={() => app.openDrawer('datasource')}
                    title="管理数据源：新增 MySQL 连接、切换激活、测试连接、标记可写档位">
              <DatabaseIcon />数据源
            </button>
            <button className={`btn-ghost btn-sm ${app.drawer === 'schema' ? 'on' : ''}`}
                    onClick={() => app.openDrawer('schema')}
                    title="浏览库表字段，勾选后作为提问上下文">
              <TableIcon />库表结构
            </button>
            <button className="btn-ghost btn-sm" onClick={() => fileRef.current?.click()}
                    disabled={upload.pending}
                    title="上传 CSV / TSV / JSON / XLSX，解析后自动激活即可提问">
              <UploadIcon />{upload.pending ? '导入中…' : '导入文件'}
            </button>
            <button className={`btn-ghost btn-sm ${app.drawer === 'tasks' ? 'on' : ''}`}
                    onClick={() => app.openDrawer('tasks')}
                    title="任务清单：收集分析给出的建议，编辑排序后统一执行">
              <ChecklistIcon />任务清单
              {pending > 0 && <span className="entry-badge">{pending}</span>}
            </button>
            <ModelSwitcher />
            <TierSwitch />
            <button className={`btn-ghost btn-sm ${app.drawer === 'write' ? 'on' : ''}`}
                    onClick={() => app.openDrawer('write')}
                    title="写 SQL：先预览影响面，勾了确认（破坏性操作还要手输表名）才真的落刀">
              <PencilIcon />写 SQL
              {app.writeEffective !== 'read' && <span className="entry-badge">写</span>}
            </button>
            <input ref={fileRef} type="file" hidden accept=".csv,.tsv,.txt,.json,.xlsx"
                   onChange={(e) => void onFile(e)} />
          </div>

          <div className="composer-ctx">
            {ctx.length > 0
              ? ctx.map((c) => <Chip key={c} tone="accent">{c}</Chip>)
              : <span className="composer-hint">未选择库表字段（将在激活库内自动识别）</span>}
          </div>
        </div>

        <button className="btn-primary" onClick={submit} disabled={!canSubmit}>
          {busy ? '执行中…' : '提问'}
        </button>
      </div>

      {!app.activeDs && (
        <p className="note note-warn">
          尚未激活数据源：点「数据源」连接 MySQL，或「导入文件」上传 CSV/Excel。
        </p>
      )}
      {upErr && <p className="note note-bad">{upErr}</p>}
    </section>
  )
}
