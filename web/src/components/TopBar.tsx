/* 顶栏：侧栏开关 + 品牌 + 激活数据源 + 模型切换。 */
import { useApp } from '../state/AppProvider'
import { BrandMark, SideToggleIcon } from './icons'
import { Chip } from './Primitives'
import { ModelSwitcher } from './ModelSwitcher'

export function TopBar() {
  const app = useApp()

  return (
    <header className="topbar">
      <div className="brand">
        <button className="side-toggle" onClick={app.toggleSide}
                title={`${app.sideOpen ? '收起' : '展开'}侧栏（Ctrl+B）`}
                aria-label={app.sideOpen ? '收起侧栏' : '展开侧栏'} aria-expanded={app.sideOpen}>
          <SideToggleIcon open={app.sideOpen} />
        </button>
        <BrandMark />
        <div className="brand-text">
          <b>问数</b>
          <span>数据分析 Agent · 只读双层确认</span>
        </div>
      </div>

      <div className="topbar-meta">
        {app.activeDs ? (
          <Chip tone="ok" >
            <span title={`${app.activeDs.host || '文件'}:${app.activeDs.port ?? ''}/${app.activeDs.database || ''}`}>
              {app.activeDs.name}
            </span>
          </Chip>
        ) : (
          <Chip tone="warn">未激活数据源</Chip>
        )}
        {/* 模型：顶栏与提问区共用同一个切换器（未接入时显示 healthz 的环境变量配置） */}
        <ModelSwitcher tone="chip" />
      </div>
    </header>
  )
}
