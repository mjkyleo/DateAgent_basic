/* 任务清单面板：把分析给出的建议变成人可编辑、可调序、可批量执行的待办。

   三条刻意的取舍：
   1) 「统一执行」不是点一下就跑：先把待执行的 SQL 摊开、勾一次确认位，
      这跟问数主链路的第二层人工确认是同一件事，批量执行等于批量确认，不能含糊；
   2) 没绑定 SQL 的任务不会被悄悄忽略：执行时逐条给出"为什么没跑"，由人标记完成；
   3) 顺序用 ↑↓ 按钮而不是拖拽：抽屉内部滚动 + 拖拽在窄屏上很难使上劲，
      而且调序结果要整份提交给后端（逐条 +1/-1 并发时会错位）。
*/
import { useMemo, useState } from 'react'
import { tasksApi } from '../../api'
import type { Task, TaskRunResult } from '../../api/types'
import { errMessage } from '../../hooks/useAsync'
import { useApp } from '../../state/AppProvider'
import { AsyncView, Chip, EmptyState, ErrorNote } from '../Primitives'
import { ConfirmDialog } from '../ConfirmDialog'
import { PlusIcon, TrashIcon } from '../icons'
import type { Tone } from '../Primitives'

const STATUS: Record<Task['status'], { tone: Tone; label: string }> = {
  todo: { tone: 'neutral', label: '待办' },
  running: { tone: 'info', label: '执行中' },
  done: { tone: 'ok', label: '已完成' },
  failed: { tone: 'bad', label: '跑失败' },
}

const SOURCE: Record<Task['source_kind'], string> = {
  manual: '手工添加',
  advice: '来自数据源洞察',
  sql_advice: '来自 SQL 建议',
}

export function TaskListPanel() {
  const app = useApp()
  const view = app.tasks
  const rows = useMemo(() => view.data ?? [], [view.data])
  const projectId = app.projectId ?? 0
  const projectName = app.projects.data?.find((p) => p.id === app.projectId)?.name
    ?? '未分组'

  const [selected, setSelected] = useState<Record<number, boolean>>({})
  const [editingId, setEditingId] = useState<number | null>(null)
  const [adding, setAdding] = useState(false)
  const [review, setReview] = useState(false)
  const [ack, setAck] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [result, setResult] = useState<TaskRunResult | null>(null)
  const [removing, setRemoving] = useState<Task | null>(null)

  async function act(fn: () => Promise<unknown>) {
    setBusy(true)
    setErr('')
    try {
      await fn()
      view.reload()
    } catch (e: unknown) {
      setErr(errMessage(e))
    } finally {
      setBusy(false)
    }
  }

  /* 没勾选就跑"全部未完成"；勾选了就只跑选中的（用户手动挑过的意图优先）。
     这里必须看"有没有勾上的"而不是"有没有勾过"：取消勾选后 selected 里会留下
     一个 false 的键，用 Object.keys().length 判断就会算出 toRun 为空、按钮显示
     「无待执行」，而提交给后端的空 task_ids 会被当成"跑全部未完成"——界面说的
     和实际做的正好相反，这在需要确认的执行动作上是不能接受的。 */
  const hasPick = useMemo(() => Object.values(selected).some(Boolean), [selected])
  const toRun = useMemo(
    () => rows.filter((t) => (hasPick ? selected[t.id] : t.status !== 'done')),
    [rows, selected, hasPick],
  )
  const withSql = toRun.filter((t) => t.sql.trim() && t.status !== 'done')
  const withoutSql = toRun.filter((t) => !t.sql.trim() || t.status === 'done')

  function move(index: number, dir: -1 | 1) {
    const next = [...rows]
    const j = index + dir
    if (j < 0 || j >= next.length) return
    ;[next[index], next[j]] = [next[j], next[index]]
    void act(() => tasksApi.reorder(projectId, next.map((t) => t.id)))
  }

  async function runAll() {
    setBusy(true)
    setErr('')
    try {
      const r = await tasksApi.run(projectId, toRun.map((t) => t.id), true)
      setResult(r)
      setSelected({})
      view.reload()
    } catch (e: unknown) {
      setErr(errMessage(e))
    } finally {
      setBusy(false)
      setReview(false)
      setAck(false)
    }
  }

  const todo = rows.filter((t) => t.status === 'todo' || t.status === 'failed').length

  return (
    <div className="task-panel">
      <div className="task-bar">
        <span className="result-sub">
          项目「{projectName}」· 共 {rows.length} 条，未完成 {todo} 条
        </span>
        <button className="btn-ghost btn-xs" disabled={busy}
                onClick={() => { setAdding(true); setEditingId(null) }}>
          <PlusIcon />新建任务
        </button>
      </div>

      {adding && (
        <TaskForm
          mode="create"
          busy={busy}
          onCancel={() => setAdding(false)}
          onSave={(body) => {
            setAdding(false)
            void act(() => tasksApi.create({ project_id: projectId, ds_id: app.activeDs?.id ?? 0, ...body }))
          }}
        />
      )}

      {err && <ErrorNote>{err}</ErrorNote>}

      <AsyncView loading={view.loading && !view.data} error={view.error}
                 isEmpty={rows.length === 0 && !adding}
                 empty={(
                   <EmptyState title="清单还是空的"
                     hint="在「数据源洞察」或「SQL 建议」里点某条建议右侧的「加入清单」，或点上方「新建任务」自己写一条。" />
                 )}>
        <ul className="task-list stagger">
          {rows.map((t, i) => (
            <li key={t.id} className={`task-row is-${t.status}`}>
              <div className="task-line">
                <input type="checkbox" className="task-pick"
                       checked={Boolean(selected[t.id])} disabled={busy || t.status === 'done'}
                       onChange={(e) => setSelected((p) => ({ ...p, [t.id]: e.target.checked }))}
                       aria-label={`选择「${t.title}」参加本次执行`} />
                <span className="task-order">
                  <button className="task-arrow" onClick={() => move(i, -1)} disabled={i === 0 || busy}
                          title="上移（执行顺序靠前）" aria-label="上移">↑</button>
                  <button className="task-arrow" onClick={() => move(i, 1)}
                          disabled={i === rows.length - 1 || busy}
                          title="下移（执行顺序靠后）" aria-label="下移">↓</button>
                </span>

                <div className="task-main">
                  <div className="task-head">
                    <Chip tone={STATUS[t.status].tone}>{STATUS[t.status].label}</Chip>
                    <b>{t.title}</b>
                    {t.source_kind !== 'manual' && (
                      <span className="task-from">{SOURCE[t.source_kind]}</span>
                    )}
                  </div>
                  {t.note && <p className="task-note">{t.note}</p>}
                  {t.sql
                    ? <p className="task-sql"><code>{t.sql}</code></p>
                    : <p className="task-need">未绑定 SQL：需要人工办结，做完点「标记完成」</p>}
                  {typeof t.source_ref?.evidence === 'string' && (
                    <p className="task-ev">
                      <span className="advice-evidence-tag">依据</span>
                      <code>{String(t.source_ref?.evidence)}</code>
                    </p>
                  )}
                  {t.last_result && (
                    <p className="task-result-line">
                      上次执行：{String(t.last_result.ds_name ?? '')}
                      {' · '}
                      {t.last_result.error
                        ? <span className="is-bad">{String(t.last_result.error)}</span>
                        : `${String(t.last_result.row_count ?? '—')} 行 · ${String(t.last_result.cost_ms ?? '—')} ms`}
                      {t.last_result.finished_at ? ` · ${String(t.last_result.finished_at)}` : ''}
                    </p>
                  )}
                </div>

                <div className="task-acts">
                  <button className="btn-ghost btn-xs" disabled={busy}
                          onClick={() => { setEditingId(editingId === t.id ? null : t.id); setAdding(false) }}>
                    编辑
                  </button>
                  {t.status === 'done'
                    ? (
                      <button className="btn-ghost btn-xs" disabled={busy}
                              onClick={() => void act(() => tasksApi.update(t.id, { status: 'todo' }))}>
                        重新打开
                      </button>
                    ) : (
                      <button className="btn-ghost btn-xs" disabled={busy}
                              onClick={() => void act(() => tasksApi.update(t.id, { status: 'done' }))}>
                        标记完成
                      </button>
                    )}
                  {t.session_id && (
                    <button className="btn-ghost btn-xs"
                            onClick={() => { app.setReplayId(t.session_id); app.closeDrawer() }}
                            title="回看这一轮的 SQL 与结果（清单里只存摘要，明细在历史）">
                      查看结果
                    </button>
                  )}
                  <button className="task-del" onClick={() => setRemoving(t)}
                          aria-label={`删除任务「${t.title}」`}><TrashIcon /></button>
                </div>
              </div>

              {editingId === t.id && (
                <TaskForm
                  mode="edit"
                  task={t}
                  busy={busy}
                  onCancel={() => setEditingId(null)}
                  onSave={(body) => {
                    setEditingId(null)
                    void act(() => tasksApi.update(t.id, body))
                  }}
                />
              )}
            </li>
          ))}
        </ul>
      </AsyncView>

      {result && (
        <div className="task-block">
          <div className="insight-block-head">
            <b>执行结果</b>
            <button className="btn-ghost btn-xs" onClick={() => setResult(null)}>收起</button>
          </div>
          <p className="result-sub">{result.msg}</p>
          <ul className="task-outcome">
            {result.items.map((it) => (
              <li key={it.id} className={`is-${it.ran ? it.status : 'skip'}`}>
                <b>{it.title}</b>
                <span>
                  {it.ran
                    ? it.status === 'done'
                      ? `已执行 · ${it.row_count ?? '—'} 行`
                      : (it.error || '执行失败')
                    : (it.reason || '未执行')}
                </span>
                {it.session_id && (
                  <button className="btn-ghost btn-xs"
                          onClick={() => { app.setReplayId(it.session_id); app.closeDrawer() }}>
                    查看结果
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="task-run">
        {review
          ? (
            <div className="task-review">
              <b>执行核对</b>
              {withSql.length > 0 && (
                <ol className="task-review-list">
                  {withSql.map((t) => <li key={t.id}><span>{t.title}</span><code>{t.sql}</code></li>)}
                </ol>
              )}
              {withSql.length === 0 && (
                <p className="note">选中的任务里没有绑定 SQL 的，本次不会有查询真正执行。</p>
              )}
              {withoutSql.length > 0 && (
                <p className="note note-warn">
                  另有 {withoutSql.length} 条未绑定 SQL 或已完成：执行时逐条跳过并说明原因，不会代替你办结。
                </p>
              )}
              <label className="task-ack">
                <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                我已核对以上 {withSql.length} 条 SQL，确认都是只读查询
              </label>
              <div className="modal-actions">
                <button className="btn-ghost btn-sm" onClick={() => { setReview(false); setAck(false) }}>
                  取消
                </button>
                <button className="btn-primary btn-sm" disabled={!ack || busy}
                        onClick={() => void runAll()}>
                  {busy ? '执行中…' : '确认执行'}
                </button>
              </div>
            </div>
          ) : (
            <>
              <button className="btn-primary btn-sm" disabled={busy || rows.length === 0}
                      onClick={() => { setReview(true); setAck(false); setResult(null) }}>
                统一执行{toRun.length ? `（${toRun.length} 条）` : ''}
              </button>
              <span className="result-sub">
                先摊开 SQL、勾一次确认才跑；数据源始终只读，写库语句会被安全门拦住。
              </span>
            </>
          )}
      </div>

      <ConfirmDialog
        open={Boolean(removing)}
        title="从清单删除这条任务？"
        target={removing?.title}
        hint="只删清单里这一行。它执行过的会话属于历史，仍然保留。"
        pending={busy}
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          const t = removing
          setRemoving(null)
          if (t) void act(() => tasksApi.remove(t.id))
        }}
      />
    </div>
  )
}

interface FormBody {
  title: string
  note: string
  question: string
  sql: string
}

/* 新建/编辑同构：字段就是任务的四项内容，SQL 留空表示"这条靠人办"。 */
function TaskForm({ mode, task, busy, onSave, onCancel }: {
  mode: 'create' | 'edit'
  task?: Task
  busy: boolean
  onSave: (body: FormBody) => void
  onCancel: () => void
}) {
  const [title, setTitle] = useState(task?.title ?? '')
  const [note, setNote] = useState(task?.note ?? '')
  const [question, setQuestion] = useState(task?.question ?? '')
  const [sql, setSql] = useState(task?.sql ?? '')
  const blank = !title.trim()

  return (
    <div className="task-form">
      <label className="field">
        <span>要办的事（必填）</span>
        <input value={title} maxLength={512} placeholder="例如：给 t_order 的 city 加索引"
               onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label className="field">
        <span>补充说明</span>
        <textarea rows={2} value={note} placeholder="为什么做、找谁做、注意事项"
                  onChange={(e) => setNote(e.target.value)} />
      </label>
      <div className="field-row">
        <label className="field">
          <span>核对用的问题（可选）</span>
          <input value={question} placeholder="执行时会作为这一轮会话的问题"
                 onChange={(e) => setQuestion(e.target.value)} />
        </label>
      </div>
      <label className="field">
        <span>SQL（可选，留空 = 人工办结）</span>
        <textarea rows={2} value={sql} placeholder="SELECT ...（只读查询才会被执行）"
                  onChange={(e) => setSql(e.target.value)} />
      </label>
      <div className="modal-actions">
        <button className="btn-ghost btn-sm" onClick={onCancel} disabled={busy}>取消</button>
        <button className="btn-primary btn-sm" disabled={blank || busy}
                onClick={() => onSave({ title: title.trim(), note, question, sql: sql.trim() })}>
          {mode === 'create' ? '加入清单' : '保存'}
        </button>
      </div>
    </div>
  )
}
