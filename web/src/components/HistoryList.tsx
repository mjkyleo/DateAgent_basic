/* 历史对话（侧栏唯一内容）：按「项目」分组，每个项目对应一个本地文件夹。
   一轮一条 —— 标题（可重命名）、时间、意图、行数与耗时、是否已出结果。
   支持：新建会话（落到当前项目）/ 回看 / 重命名 / 移动项目 / 删除（确认模态框）。
   分组用原生 <details>：默认展开、无需自己维护折叠状态，键盘也能用。 */
import { useMemo, useState } from 'react'
import { useApp } from '../state/AppProvider'
import { useRound } from '../session/RoundProvider'
import { errMessage, useAction } from '../hooks/useAsync'
import { projectsApi, roundsApi } from '../api'
import type { Project, RoundSummary } from '../api/types'
import { AsyncView, Chip, Spinner } from './Primitives'
import { ConfirmDialog } from './ConfirmDialog'
import { FolderIcon, PencilIcon, PlusIcon } from './icons'

/** ISO 字符串 → "MM-DD HH:mm"；解析失败原样返回（后端改格式也不会白屏） */
export function formatTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 会话展示名：优先用户起的标题，退回问题原文，最后退回"空会话" */
function labelOf(r: RoundSummary): string {
  const title = (r.title || '').trim()
  if (title) return title
  const q = (r.user_question || '').trim()
  if (q) return q
  return '（新会话 · 未提问）'
}

export function HistoryList() {
  const app = useApp()
  const { state, reset, newSession } = useRound()
  const remove = useAction((sid: string) => roundsApi.remove(sid))
  const [pendingDelete, setPendingDelete] = useState<RoundSummary | null>(null)
  const [pendingProject, setPendingProject] = useState<Project | null>(null)
  const [creating, setCreating] = useState(false)
  const [createErr, setCreateErr] = useState('')
  const [renaming, setRenaming] = useState<string | null>(null)
  const [moving, setMoving] = useState<string | null>(null)
  const [showNewProject, setShowNewProject] = useState(false)
  const [newProject, setNewProject] = useState({ name: '', folder: '' })
  const [projectErr, setProjectErr] = useState('')

  const rounds = app.rounds.data?.rounds ?? []
  const projects = app.projects.data ?? []

  /* 按项目分组。项目顺序保持后端给的原序（未分组在最前），
     组内按 updated_at 倒序 —— 最近问过的排前面。 */
  const grouped = useMemo(() => {
    const buckets = new Map<number, RoundSummary[]>()
    for (const p of projects) buckets.set(p.id, [])
    for (const r of rounds) {
      const list = buckets.get(r.project_id)
      if (list) list.push(r)
      else buckets.set(r.project_id, [r])   // 项目刚被删但列表还没刷新
    }
    return [...buckets.entries()].map(([pid, items]) => ({
      project: projects.find((p) => p.id === pid) ?? null,
      projectId: pid,
      items: [...items].sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
    }))
  }, [rounds, projects])

  async function onRemove() {
    if (!pendingDelete) return
    await remove.run(pendingDelete.session_id)
    if (remove.error) return
    setPendingDelete(null)
    app.rounds.reload()
    app.projects.reload()
    if (app.replayId === pendingDelete.session_id) app.setReplayId(null)
    if (state.sessionId === pendingDelete.session_id) reset()
  }

  async function onCreate() {
    setCreateErr('')
    setCreating(true)
    try {
      await newSession()
      app.setReplayId(null)
    } catch (e: unknown) {
      setCreateErr(errMessage(e))
    } finally {
      setCreating(false)
    }
  }

  async function createProject() {
    setProjectErr('')
    try {
      const p = await projectsApi.create({
        name: newProject.name.trim(),
        folder_path: newProject.folder.trim(),
      })
      setNewProject({ name: '', folder: '' })
      setShowNewProject(false)
      app.projects.reload()
      app.setProjectId(p.id)      // 建完即切到它，下一步提问就归到这里
    } catch (e: unknown) {
      setProjectErr(errMessage(e))
    }
  }

  async function removeProject() {
    if (!pendingProject) return
    try {
      await projectsApi.remove(pendingProject.id)
      if (app.projectId === pendingProject.id) app.setProjectId(null)
      setPendingProject(null)
      app.projects.reload()
      app.rounds.reload()
    } catch (e: unknown) {
      setProjectErr(errMessage(e))
    }
  }

  const currentProject = projects.find((p) => p.id === app.projectId) ?? null

  return (
    <div className="history">
      <div className="history-head">
        <b>历史对话</b>
        <span className="history-head-actions">
          <button className="btn-ghost btn-xs" onClick={() => void onCreate()}
                  disabled={creating || !app.activeDs}
                  title={currentProject
                    ? `新建到项目「${currentProject.name}」`
                    : '新建一条空会话，然后直接提问'}>
            {creating ? <Spinner /> : <PlusIcon />}新建
          </button>
          <button className="btn-ghost btn-xs" onClick={() => setShowNewProject((v) => !v)}
                  title="新建项目（= 一个本地文件夹）">
            <FolderIcon />项目
          </button>
          <button className="btn-ghost btn-xs" onClick={app.rounds.reload}
                  disabled={app.rounds.loading} title="重新拉取列表">刷新</button>
        </span>
      </div>

      {currentProject && (
        <p className="history-scope">
          新建对话将归入 <b>{currentProject.name}</b>
          <button className="link-btn" onClick={() => app.setProjectId(null)}>改回未分组</button>
        </p>
      )}

      {showNewProject && (
        <div className="project-new">
          <label>
            <span>项目名</span>
            <input value={newProject.name} placeholder="留空则取文件夹名"
                   onChange={(e) => setNewProject((v) => ({ ...v, name: e.target.value }))} />
          </label>
          <label>
            <span>文件夹</span>
            <input value={newProject.folder} placeholder="留空自动建在 data/projects 下"
                   onChange={(e) => setNewProject((v) => ({ ...v, folder: e.target.value }))} />
          </label>
          <div className="project-new-actions">
            <button className="btn-ghost btn-xs" onClick={() => setShowNewProject(false)}>取消</button>
            <button className="btn-primary btn-xs" onClick={() => void createProject()}
                    disabled={!newProject.name.trim() && !newProject.folder.trim()}>
              创建项目
            </button>
          </div>
          {projectErr && <p className="note note-bad">{projectErr}</p>}
        </div>
      )}

      <AsyncView loading={app.rounds.loading} error={app.rounds.error}
        isEmpty={rounds.length === 0}
        empty={<p className="panel-hint">还没有记录。点「新建」或直接提问，每一轮都会留在这里。</p>}>
        <div className="project-groups stagger">
          {grouped.map(({ project, projectId, items }) => {
            const pid = project?.id ?? projectId
            const isCurrent = app.projectId === pid
            return (
              <details key={pid} className="project-group" open>
                <summary className="project-group-head">
                  <FolderIcon />
                  <b>{project?.name ?? '（已删除的项目）'}</b>
                  <span className="project-count">{items.length}</span>
                  {project?.is_default && <Chip tone="neutral">默认</Chip>}
                  <span className="project-group-actions">
                    <button className="btn-ghost btn-xs" title="在这个项目里新建对话"
                            onClick={(e) => { e.preventDefault(); app.setProjectId(pid) }}>
                      {isCurrent ? '当前' : '设为当前'}
                    </button>
                    {project && !project.is_default && (
                      <button className="btn-ghost btn-xs btn-danger" title="删除项目"
                              onClick={(e) => { e.preventDefault(); setPendingProject(project) }}>
                        ×
                      </button>
                    )}
                  </span>
                </summary>
                {items.length === 0 && <p className="panel-hint project-empty">这个项目还没有对话</p>}
                <ul className="history-list">
                  {items.map((r) => (
                    <HistoryItem key={r.session_id} round={r}
                      active={app.replayId === r.session_id}
                      current={state.sessionId === r.session_id
                               || state.newSessionId === r.session_id}
                      renaming={renaming === r.session_id}
                      moving={moving === r.session_id}
                      projects={projects}
                      onOpen={() => app.setReplayId(r.session_id)}
                      onStartRename={() => setRenaming(r.session_id)}
                      onRename={(title) => {
                        void roundsApi.rename(r.session_id, title)
                          .then(() => { setRenaming(null); app.rounds.reload() })
                      }}
                      onCancelRename={() => setRenaming(null)}
                      onStartMove={() => setMoving(r.session_id)}
                      onMove={(target) => {
                        void roundsApi.move(r.session_id, target)
                          .then(() => {
                            setMoving(null)
                            app.rounds.reload()
                            app.projects.reload()
                          })
                      }}
                      onCancelMove={() => setMoving(null)}
                      onRemove={() => setPendingDelete(r)} />
                  ))}
                </ul>
              </details>
            )
          })}
        </div>
      </AsyncView>

      {app.replayId && (
        <button className="btn-ghost btn-sm history-back" onClick={() => app.setReplayId(null)}>
          ← 返回当前对话
        </button>
      )}
      {!app.replayId && (state.stages.parse.attempts.length > 0 || state.newSessionId) && (
        <button className="btn-ghost btn-sm history-back" onClick={reset}>结束当前轮</button>
      )}
      {createErr && <p className="note note-bad">{createErr}</p>}
      {remove.error && <p className="note note-bad">{errMessage(remove.error)}</p>}

      <ConfirmDialog
        open={pendingDelete !== null}
        title="确认删除这一轮记录？"
        target={pendingDelete ? labelOf(pendingDelete) : undefined}
        hint="删除后不可恢复，包含这一轮的问题、SQL 与查询结果。"
        pending={remove.pending}
        onConfirm={() => void onRemove()}
        onCancel={() => setPendingDelete(null)}
      />

      <ConfirmDialog
        open={pendingProject !== null}
        title="确认删除这个项目？"
        target={pendingProject ? `${pendingProject.name}（${pendingProject.folder_path}）` : undefined}
        hint="项目内的对话会移回「未分组」，不会删除对话；磁盘上的文件夹也不会被删除。"
        onConfirm={() => void removeProject()}
        onCancel={() => setPendingProject(null)}
      />
    </div>
  )
}

function HistoryItem({ round, active, current, renaming, moving, projects,
                       onOpen, onStartRename, onRename, onCancelRename,
                       onStartMove, onMove, onCancelMove, onRemove }: {
  round: RoundSummary
  active: boolean
  current: boolean
  renaming: boolean
  moving: boolean
  projects: Project[]
  onOpen: () => void
  onStartRename: () => void
  onRename: (title: string) => void
  onCancelRename: () => void
  onStartMove: () => void
  onMove: (projectId: number) => void
  onCancelMove: () => void
  onRemove: () => void
}) {
  const empty = !round.user_question.trim()
  const [title, setTitle] = useState(round.title || labelOf(round))

  if (renaming) {
    return (
      <li className="history-item is-editing">
        <input className="history-rename" value={title} autoFocus
               onChange={(e) => setTitle(e.target.value)}
               onKeyDown={(e) => {
                 if (e.key === 'Enter' && title.trim()) onRename(title.trim())
                 if (e.key === 'Escape') onCancelRename()
               }} />
        <span className="history-item-actions">
          <button className="btn-ghost btn-xs" onClick={onCancelRename}>取消</button>
          <button className="btn-primary btn-xs" disabled={!title.trim()}
                  onClick={() => onRename(title.trim())}>保存</button>
        </span>
      </li>
    )
  }

  if (moving) {
    return (
      <li className="history-item is-editing">
        <span className="history-move-label">移动到：</span>
        <select className="history-move" defaultValue="" autoFocus
                onChange={(e) => { if (e.target.value) onMove(Number(e.target.value)) }}>
          <option value="" disabled>选择项目</option>
          {projects.filter((p) => p.id !== round.project_id).map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <button className="btn-ghost btn-xs" onClick={onCancelMove}>取消</button>
      </li>
    )
  }

  return (
    <li className={`history-item ${active ? 'on' : ''} ${empty ? 'is-empty' : ''}`}>
      <button className="history-main" onClick={onOpen}
              title={empty ? '这是新建的空会话，尚未提问' : labelOf(round)}>
        <span className="history-q">{labelOf(round)}</span>
        <span className="history-meta">
          <span className="history-time">{formatTime(round.created_at)}</span>
          {empty
            ? <Chip tone="neutral">空会话</Chip>
            : round.has_result
              ? <Chip tone="ok">{round.row_count ?? 0} 行 · {round.cost_ms ?? 0} ms</Chip>
              : <Chip tone="warn">未执行</Chip>}
          {current && <Chip tone="accent">当前</Chip>}
        </span>
        {round.intent && <span className="history-intent">{round.intent}</span>}
      </button>
      <span className="history-item-actions">
        <button className="history-act" onClick={onStartRename} title="重命名"
                aria-label="重命名"><PencilIcon /></button>
        <button className="history-act" onClick={onStartMove} title="移动到其他项目"
                aria-label="移动到其他项目"><FolderIcon /></button>
        <button className="history-act history-del" onClick={onRemove} title="删除这一轮记录"
                aria-label="删除这一轮记录">×</button>
      </span>
    </li>
  )
}
