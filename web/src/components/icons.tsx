/* 内联图标（不引图标库）：几何线条，1.6 描边，随 currentColor。 */
import type { EventKind } from '../session/machine'

const S = { width: 14, height: 14, viewBox: '0 0 16 16', 'aria-hidden': true } as const

export function EventIcon({ kind }: { kind: EventKind }) {
  switch (kind) {
    case 'think':
      return (
        <svg {...S}>
          <path d="M8 1.6v2.6M8 11.8v2.6M1.6 8h2.6M11.8 8h2.6M3.5 3.5l1.9 1.9M10.6 10.6l1.9 1.9M12.5 3.5l-1.9 1.9M5.4 10.6l-1.9 1.9"
                fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
          <circle cx="8" cy="8" r="2.4" fill="none" stroke="currentColor" strokeWidth="1.4" />
        </svg>
      )
    case 'tool':
      return (
        <svg {...S}>
          <path d="M9.6 2.4a3.1 3.1 0 0 1 4 4L6.1 13.9a2 2 0 0 1-2.8-2.8l7.5-7.5z"
                fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
          <path d="M7.2 4.8 11.2 8.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
        </svg>
      )
    case 'confirm':
      return (
        <svg {...S}>
          <circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <path d="M6.4 5.6v4.8M9.6 5.6v4.8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      )
    case 'output':
      return (
        <svg {...S}>
          <path d="M2.6 13.4V9.2M6.4 13.4V4.8M10.2 13.4V7.4M13.8 13.4V2.6"
                fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
        </svg>
      )
    case 'error':
      return (
        <svg {...S}>
          <circle cx="8" cy="8" r="6.2" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <path d="M8 4.8v4M8 10.8v.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      )
    default:
      return null
  }
}

export function BrandMark() {
  return (
    <svg className="mark" viewBox="0 0 28 28" aria-hidden="true">
      <rect x="1.5" y="1.5" width="25" height="25" rx="7" fill="none"
            stroke="currentColor" strokeWidth="2" />
      <path d="M7 18 L12 10 L16 14 L21 8" fill="none" stroke="currentColor"
            strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="21" cy="8" r="2" fill="currentColor" />
    </svg>
  )
}

/** 阶段导航面板的展开/收起箭头 */
export function ChevronIcon({ dir }: { dir: 'left' | 'right' }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d={dir === 'left' ? 'M10 3.5 L5.5 8 L10 12.5' : 'M6 3.5 L10.5 8 L6 12.5'}
            fill="none" stroke="currentColor" strokeWidth="1.8"
            strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function SideToggleIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <rect x="2.5" y="3.5" width="15" height="13" rx="3" fill="none"
            stroke="currentColor" strokeWidth="1.6" />
      <path d="M8 3.5 V16.5" stroke="currentColor" strokeWidth="1.6" />
      {open
        ? <path d="M13.5 7.5 L11.5 10 L13.5 12.5" fill="none" stroke="currentColor"
                strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        : <path d="M10 10 H15.5 M13 8 L15 10 L13 12" fill="none" stroke="currentColor"
                strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />}
    </svg>
  )
}

/* ---------------- 数据类图标（16 视框，与事件图标同风格） ---------------- */
const D = { width: 14, height: 14, viewBox: '0 0 16 16', 'aria-hidden': true } as const

/** 复制 */
export function CopyIcon() {
  return (
    <svg {...D}>
      <rect x="5.4" y="5.4" width="8.2" height="8.2" rx="1.4" fill="none"
            stroke="currentColor" strokeWidth="1.4" />
      <path d="M10.6 3.6a1.4 1.4 0 0 0-1.4-1.4H4a1.4 1.4 0 0 0-1.4 1.4v5.2a1.4 1.4 0 0 0 1.4 1.4"
            fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}

/** 重跑 / 刷新 */
export function RefreshIcon() {
  return (
    <svg {...D}>
      <path d="M13.2 8a5.2 5.2 0 1 1-1.6-3.7" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinecap="round" />
      <path d="M13.4 2.4v2.6h-2.6" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** 数据源（数据库） */
export function DatabaseIcon() {
  return (
    <svg {...D}>
      <ellipse cx="8" cy="3.4" rx="5" ry="1.9" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M3 3.4v4.4c0 1.05 2.24 1.9 5 1.9s5-.85 5-1.9V3.4" fill="none"
            stroke="currentColor" strokeWidth="1.4" />
      <path d="M3 7.8v4.4c0 1.05 2.24 1.9 5 1.9s5-.85 5-1.9V7.8" fill="none"
            stroke="currentColor" strokeWidth="1.4" />
    </svg>
  )
}

/** 库表结构（表格） */
export function TableIcon() {
  return (
    <svg {...D}>
      <rect x="1.8" y="2.6" width="12.4" height="10.8" rx="1.4" fill="none"
            stroke="currentColor" strokeWidth="1.4" />
      <path d="M1.8 6.2h12.4M6 6.2v7.2" fill="none" stroke="currentColor" strokeWidth="1.4" />
    </svg>
  )
}

/** 筛选 / 过滤 */
export function FilterIcon() {
  return (
    <svg {...D}>
      <path d="M2.4 3.6h11.2M4.6 8h6.8M6.8 12.4h2.4" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

/** 图表 */
export function ChartIcon() {
  return (
    <svg {...D}>
      <path d="M2.4 13.4V2.6M2.4 13.4h11.2" fill="none" stroke="currentColor"
            strokeWidth="1.4" strokeLinecap="round" />
      <path d="M5.4 10.6V7.2M8.2 10.6V4.6M11 10.6V8.6" fill="none" stroke="currentColor"
            strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

/** 文件导入（上传） */
export function UploadIcon() {
  return (
    <svg {...D}>
      <path d="M8 10.4V2.8M5.2 5.6 8 2.8l2.8 2.8" fill="none" stroke="currentColor"
            strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M2.6 10.6v2.2c0 .66.54 1.2 1.2 1.2h8.4c.66 0 1.2-.54 1.2-1.2v-2.2"
            fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}

/** 历史 / 时间 */
export function ClockIcon() {
  return (
    <svg {...D}>
      <circle cx="8" cy="8" r="5.8" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M8 4.8V8l2.4 1.6" fill="none" stroke="currentColor" strokeWidth="1.4"
            strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** 新建 / 加号 */
export function PlusIcon() {
  return (
    <svg {...D}>
      <path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" strokeWidth="1.6"
            strokeLinecap="round" />
    </svg>
  )
}

/** 清单（任务列表入口） */
export function ChecklistIcon() {
  return (
    <svg {...D}>
      <path d="M2.2 4.4l1.3 1.3 2.4-2.6M2.2 10.4l1.3 1.3 2.4-2.6" fill="none"
            stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
            strokeLinejoin="round" />
      <path d="M7.8 4.6h6M7.8 10.6h6" fill="none" stroke="currentColor"
            strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}

/** 对勾（当前选中 / 测试通过） */
export function CheckIcon() {
  return (
    <svg {...D}>
      <path d="M3.2 8.4 L6.4 11.6 L12.8 5.2" fill="none" stroke="currentColor"
            strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** 垃圾桶（删除） */
export function TrashIcon() {
  return (
    <svg {...D}>
      <path d="M2.8 4.4h10.4M6.4 4.4V2.8h3.2v1.6" fill="none" stroke="currentColor"
            strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M4.2 4.4l.7 8.2c.04.5.46.9.96.9h4.28c.5 0 .92-.4.96-.9l.7-8.2"
            fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M6.6 6.8v4.2M9.4 6.8v4.2" fill="none" stroke="currentColor"
            strokeWidth="1.3" strokeLinecap="round" />
    </svg>
  )
}

/** 文件夹（项目） */
export function FolderIcon() {
  return (
    <svg {...D}>
      <path d="M1.8 4.2c0-.66.54-1.2 1.2-1.2h3.1l1.5 1.7h4.4c.66 0 1.2.54 1.2 1.2v6.3c0 .66-.54 1.2-1.2 1.2H3c-.66 0-1.2-.54-1.2-1.2V4.2z"
            fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  )
}

/** 改名（铅笔） */
export function PencilIcon() {
  return (
    <svg {...D}>
      <path d="M11.2 2.9l1.9 1.9-7.6 7.6-2.5.6.6-2.5 7.6-7.6z" fill="none"
            stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </svg>
  )
}

/** 模型（芯片/AI） */
export function ModelIcon() {
  return (
    <svg {...D}>
      <rect x="4.6" y="4.6" width="6.8" height="6.8" rx="1.6" fill="none"
            stroke="currentColor" strokeWidth="1.4" />
      <rect x="7" y="7" width="2" height="2" rx=".5" fill="currentColor" />
      <path d="M7 4.6V2.8M7 11.2v1.8M4.6 7H2.8M11.2 7h1.8"
            fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}
