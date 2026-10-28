/* 应用级状态：健康信息、数据源清单、历史轮次、库表上下文选择、界面开关。
   这一层只放"跨页面共享且后端有落点"的东西；链路流转在 session/RoundProvider。 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from 'react'
import { datasourceApi, opsApi, projectsApi, providersApi, roundsApi, tasksApi } from '../api'
import type {
  Datasource, HealthResponse, PermTier, Project, Provider, ProviderModelItem,
  RoundSummary, Task, TaskCreate,
} from '../api/types'
import { useAsync, useAction } from '../hooks/useAsync'
import { effectiveTier, sourceCeiling } from '../lib/writeGate'

export interface Selection {
  db: string
  tables: string[]
  fields: string[]
}

export type DrawerKind = 'datasource' | 'schema' | 'providers' | 'projects' | 'tasks' | 'write' | null

/** 提问侧的写档位：只在本地记住，每次请求都当作"人自己选的"发给后端，
    后端再按数据源上限压一次 —— 界面不能替后端抬上限。 */
const TIER_KEY = 'dd.writeTier'

function readTier(raw: string | null): PermTier {
  return raw === 'write_dml' || raw === 'write_ddl' ? raw : 'read'
}

interface AppValue {
  health: ReturnType<typeof useAsync<HealthResponse>>
  datasources: ReturnType<typeof useAsync<Datasource[]>>
  activeDs: Datasource | null
  /** 写档位（提问区左下角那个开关）与它在本数据源上的实际生效值 */
  writeTier: PermTier
  setWriteTier: (t: PermTier) => void
  writeCeiling: PermTier
  writeEffective: PermTier
  /** 数据源可写标记（allowed_ops）：人给的源上限，文件源后端会直接拒 */
  markDsOps: (id: number, ops: PermTier) => Promise<void>
  /** 供应商接入清单与当前使用的模型（后端 MySQL 元数据库；不可用时为空列表） */
  providers: ReturnType<typeof useAsync<Provider[]>>
  activeProvider: Provider | null
  activeModel: ProviderModelItem | null
  /** 项目（会话分组，每个项目对应一个本地文件夹） */
  projects: ReturnType<typeof useAsync<Project[]>>
  /** 当前项目：新建会话与提问都归到它；空 = 由后端落到「未分组」 */
  projectId: number | null
  setProjectId: (id: number | null) => void
  rounds: ReturnType<typeof useAsync<{ rounds: RoundSummary[] }>>
  refreshRounds: () => void
  /** 任务清单：按项目一份（后端 MySQL project_tasks；元数据库不可用时 503） */
  tasks: ReturnType<typeof useAsync<Task[]>>
  /** 把一条建议或自拟的待办加进当前项目清单（建议原文进 source_ref 快照） */
  addTask: (body: Omit<TaskCreate, 'project_id'>) => Promise<Task | undefined>
  switchDs: (id: number) => Promise<void>
  removeDs: (id: number) => Promise<void>
  importFile: (file: File) => Promise<void>
  selection: Selection
  setSelection: (s: Selection) => void
  /* 输入框草稿：全局持有，这样「返回修改」「点击示例」都能把问题写回输入框 */
  draft: string
  setDraft: (s: string) => void
  sideOpen: boolean
  toggleSide: () => void
  drawer: DrawerKind
  openDrawer: (k: Exclude<DrawerKind, null>) => void
  closeDrawer: () => void
  replayId: string | null          // 正在回看的历史轮次（null = 看当前轮）
  setReplayId: (id: string | null) => void
}

const AppCtx = createContext<AppValue | undefined>(undefined)

export function AppProvider({ children }: { children: ReactNode }) {
  const health = useAsync(() => opsApi.health(), [])
  const datasources = useAsync(() => datasourceApi.list(), [])
  /* 一次拉满：历史按项目分组渲染，分页会把某个项目的会话切掉半截 */
  const rounds = useAsync(() => roundsApi.list(200), [])
  /* 供应商/项目清单失败（元数据库未连）不阻塞整体：列表为空时面板给出提示 */
  const providers = useAsync(() => providersApi.list(), [])
  const projects = useAsync(() => projectsApi.list(), [])

  const [selection, setSelection] = useState<Selection>({ db: '', tables: [], fields: [] })
  const [draft, setDraft] = useState('')
  /* 窄屏（≤900px）侧栏是浮层，会盖住对话区 —— 默认收起，需要时再呼出 */
  const [sideOpen, setSideOpen] = useState(() => window.innerWidth > 900)
  const [drawer, setDrawer] = useState<DrawerKind>(null)
  const [replayId, setReplayId] = useState<string | null>(null)
  /* 当前项目要持久化：只是 React state 的话，刷新后静默回到「未分组」，
     新会话就落到别的项目里去了 —— 用户看不到任何提示，只会觉得"分类失效"。 */
  const [projectId, setProjectIdState] = useState<number | null>(() => {
    const raw = localStorage.getItem('dd.projectId')
    const n = raw ? Number(raw) : NaN
    return Number.isFinite(n) && n > 0 ? n : null
  })
  const setProjectId = useCallback((id: number | null) => {
    setProjectIdState(id)
    if (id) localStorage.setItem('dd.projectId', String(id))
    else localStorage.removeItem('dd.projectId')
  }, [])

  const activeDs = useMemo(
    () => datasources.data?.find((d) => d.is_active) ?? null,
    [datasources.data],
  )

  /* 写档位本地记住：切数据源不重置（人常常在几个源之间来回切）。
     发给后端的是这份"人自己选的"，后端再按源上限压一次；这里算的生效值只用于界面提示。 */
  const [writeTier, setWriteTierState] = useState<PermTier>(
    () => readTier(localStorage.getItem(TIER_KEY)))
  const setWriteTier = useCallback((t: PermTier) => {
    setWriteTierState(t)
    if (t === 'read') localStorage.removeItem(TIER_KEY)
    else localStorage.setItem(TIER_KEY, t)
  }, [])
  const writeCeiling = useMemo(() => sourceCeiling(activeDs), [activeDs])
  /* 生效值要把总闸算进去：总闸关着时按钮必须显示只读，
     否则界面承诺了一个后端不会兑现的档位。 */
  const writesEnabled = health.data?.writes_enabled ?? null
  const writeEffective = useMemo(
    () => effectiveTier(writeTier, writeCeiling, writesEnabled),
    [writeTier, writeCeiling, writesEnabled])

  const markDsOps = useCallback(async (id: number, ops: PermTier) => {
    await datasourceApi.update(id, { allowed_ops: ops })
    datasources.reload()
  }, [datasources])
  /* 当前模型 = 唯一 is_active 的那个（后端保证全局唯一），所属供应商即 activeProvider */
  const activePick = useMemo(() => {
    for (const p of providers.data ?? []) {
      const m = p.models.find((x) => x.is_active)
      if (m) return { provider: p, model: m }
    }
    return { provider: null, model: null }
  }, [providers.data])
  const activeProvider = activePick.provider
  const activeModel = activePick.model

  /* 记住的项目被删掉后要回落：否则提问会带上一个不存在的 project_id，
     后端 _project_of 会照单全收，会话就挂到孤儿 id 上。 */
  const projectList = projects.data
  useEffect(() => {
    if (projectId && projectList && projectList.length > 0
        && !projectList.some((p) => p.id === projectId)) {
      setProjectIdState(null)
      localStorage.removeItem('dd.projectId')
    }
  }, [projectId, projectList])

  const refreshRounds = rounds.reload

  /* 清单跟着当前项目走：切换项目就是切换清单（后端按 project_id 存） */
  const tasks = useAsync(() => tasksApi.list(projectId), [projectId])

  const addTask = useCallback(async (body: Omit<TaskCreate, 'project_id'>) => {
    const row = await tasksApi.create({ project_id: projectId ?? 0, ...body })
    tasks.reload()
    return row
  }, [projectId, tasks])

  const switchDs = useCallback(async (id: number) => {
    await datasourceApi.switchTo(id)
    datasources.reload()
  }, [datasources])

  const removeDs = useCallback(async (id: number) => {
    await datasourceApi.remove(id)
    datasources.reload()
  }, [datasources])

  const importFile = useCallback(async (file: File) => {
    await datasourceApi.importFile(file)
    datasources.reload()
    rounds.reload()
  }, [datasources, rounds])

  const value: AppValue = {
    health, datasources, activeDs,
    writeTier, setWriteTier, writeCeiling, writeEffective, markDsOps,
    providers, activeProvider, activeModel,
    projects, projectId, setProjectId,
    rounds, refreshRounds,
    tasks, addTask,
    switchDs, removeDs, importFile,
    selection, setSelection, draft, setDraft,
    sideOpen, toggleSide: () => setSideOpen((v) => !v),
    drawer, openDrawer: setDrawer, closeDrawer: () => setDrawer(null),
    replayId, setReplayId,
  }

  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>
}

export function useApp(): AppValue {
  const v = useContext(AppCtx)
  if (!v) throw new Error('useApp 必须在 AppProvider 内使用')
  return v
}

/* 数据源的测试连接：每个源各自用一个 action，避免共享 pending 导致整列转圈 */
export function useTestConnection() {
  const [results, setResults] = useState<Record<number, { ok: boolean; msg: string }>>({})
  const [pendingId, setPendingId] = useState<number | null>(null)
  const run = useCallback(async (id: number) => {
    setPendingId(id)
    try {
      const r = await datasourceApi.test(id)
      setResults((prev) => ({ ...prev, [id]: { ok: r.connected, msg: r.msg } }))
    } catch (e: unknown) {
      setResults((prev) => ({
        ...prev, [id]: { ok: false, msg: e instanceof Error ? e.message : '测试失败' },
      }))
    } finally {
      setPendingId(null)
    }
  }, [])
  return { run, results, pendingId }
}

/* 供应商的测试连接：与数据源同一套形态（逐项 pending + 结果按 id 存）。
   测试会真实请求厂商端点，超时给到 40s；传 model 才验证具体模型是否可调用。 */
export function useTestProvider() {
  const [results, setResults] = useState<Record<number, { ok: boolean; msg: string }>>({})
  const [pendingId, setPendingId] = useState<number | null>(null)
  const run = useCallback(async (id: number, model = '') => {
    setPendingId(id)
    try {
      const r = await providersApi.test(id, model)
      setResults((prev) => ({ ...prev, [id]: { ok: r.connected, msg: r.msg } }))
    } catch (e: unknown) {
      setResults((prev) => ({
        ...prev, [id]: { ok: false, msg: e instanceof Error ? e.message : '测试失败' },
      }))
    } finally {
      setPendingId(null)
    }
  }, [])
  return { run, results, pendingId }
}

export { useAction }
