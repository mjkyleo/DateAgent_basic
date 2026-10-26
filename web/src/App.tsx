/* 工作台装配：
   左栏只放历史对话；右栏按"阶段"组织这一轮 —— 一次只展开一个阶段，
   其余收起为可切换列表；阶段导航是悬浮在页面之上的浮动面板，可收起。
   数据源与库表结构收在底部输入区的弹出面板里。 */
import { useEffect } from 'react'
import { AppProvider, useApp } from './state/AppProvider'
import { RoundProvider, useRound } from './session/RoundProvider'
import { TopBar } from './components/TopBar'
import { HistoryList } from './components/HistoryList'
import { Composer } from './components/Composer'
import { ReplayView } from './components/ReplayView'
import { StagePanel } from './components/stages/StagePanel'
import { StageNavigator } from './components/StageNavigator'
import { DataSourcePanel } from './components/DataSourcePanel'
import { SchemaPanel } from './components/SchemaPanel'
import { ProviderPanel } from './components/ProviderPanel'
import { TaskListPanel } from './components/tasks/TaskListPanel'
import { Sheet } from './components/Sheet'

const EXAMPLES = [
  '统计 2025 年每月订单金额，看趋势',
  '各平台订单额对比',
  '销售额前 10 的商品是哪些',
]

export function App() {
  return (
    <AppProvider>
      <RoundProvider>
        <Workbench />
      </RoundProvider>
    </AppProvider>
  )
}

function Workbench() {
  const app = useApp()
  const { state } = useRound()

  /* 侧栏快捷键（对标豆包/DeepSeek）；输入框内不拦截 */
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault()
        app.toggleSide()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [app])

  // 一轮 = 解析意图阶段已有尝试；空态下不显示阶段导航
  const inRound = state.stages.parse.attempts.length > 0
  // 刚「新建会话」落了一条空会话：同样进空态（欢迎区），只是多一句提示
  const freshSession = !inRound && Boolean(state.newSessionId)

  return (
    <div className="workbench">
      <TopBar />
      <div className="workbench-body">
        <aside className={`rail ${app.sideOpen ? '' : 'is-hidden'}`}
               aria-hidden={!app.sideOpen}>
          <HistoryList />
        </aside>

        <main className="stage">
          <div className="stage-scroll">
            <div className="stage-inner">
              {app.replayId
                ? <ReplayView sessionId={app.replayId} />
                : inRound
                  ? <StagePanel />
                  : <Welcome fresh={freshSession} onPick={(q) => app.setDraft(q)} />}
            </div>
          </div>

          {/* 输入区停靠在底部；弹出面板相对它向上展开 */}
          <div className="dock">
            <div className="stage-inner">
              <Composer />
              <Sheet open={app.drawer === 'datasource'} title="数据源"
                     hint="连接、切换、测试；文件导入在输入区"
                     onClose={app.closeDrawer}>
                <DataSourcePanel />
              </Sheet>
              <Sheet open={app.drawer === 'schema'} title="库表结构"
                     hint="勾选表/字段作为提问上下文"
                     onClose={app.closeDrawer}>
                <SchemaPanel />
              </Sheet>
              <Sheet open={app.drawer === 'providers'} title="模型供应商"
                     hint="接入多家厂商（OpenAI 兼容），测试连通后保存"
                     onClose={app.closeDrawer}>
                <ProviderPanel />
              </Sheet>
              <Sheet open={app.drawer === 'tasks'} title="任务清单"
                     hint="把分析给出的建议收进来：可编辑、可调序、核对后统一执行"
                     onClose={app.closeDrawer}>
                <TaskListPanel />
              </Sheet>
            </div>
          </div>
        </main>
      </div>

      {/* 悬浮在页面之上的阶段导航：有轮次时才出现 */}
      {inRound && !app.replayId && <StageNavigator />}
    </div>
  )
}

function Welcome({ onPick, fresh = false }: { onPick: (q: string) => void; fresh?: boolean }) {
  const app = useApp()
  return (
    <div className="welcome animate-in">
      <b>{fresh ? '新会话已就绪' : '开始问数'}</b>
      <p>用自然语言提问即可。Agent 会分五个阶段推进：解析意图 → 生成 SQL →
         人工确认 → 执行查询 → 分析输出；每一步都能点开看它在做什么，也能退回重改。</p>
      {fresh && (
        <p className="note note-warn">
          已在历史里新建一条空会话，本次提问会绑定到它（不会额外产生空白记录）。
        </p>
      )}
      {!app.activeDs && (
        <p className="note note-warn">
          还没有激活数据源：点输入区的「数据源」连接 MySQL，或「导入文件」上传 CSV/Excel。
        </p>
      )}
      <div className="suggest-list">
        {EXAMPLES.map((q) => (
          <button key={q} className="suggest" onClick={() => onPick(q)}>{q}</button>
        ))}
      </div>
    </div>
  )
}
