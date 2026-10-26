/* ECharts 薄封装：按需引入（只注册用到的图表与组件），避免整包 1MB+。
   后端直接给 ECharts option（chart_option 字段），这里负责渲染、自适应，
   以及把 option 改造成 Data Dense 观感（紧凑坐标轴 / 细网格 / 规范色板 / 淡工具提示）。
   后端只给数据与图表类型，视觉规格统一在前端收敛 —— 改样式只动这一个文件。 */
import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import {
  GridComponent, LegendComponent, TitleComponent, TooltipComponent, DatasetComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

/* 按需注册：后端 build_chart_option 会用到 title / tooltip / grid / legend（见
   core/viz_recommender.py）。少注册一个组件，ECharts 只会在控制台报错而不渲染对应部分，
   所以这里与后端用到的组件保持一一对应。 */
echarts.use([
  BarChart, LineChart, PieChart,
  GridComponent, LegendComponent, TitleComponent, TooltipComponent, DatasetComponent,
  CanvasRenderer,
])

/* 色板与 tokens.css 的 --chart-* 一致（ECharts 只认具体色值，读不到 CSS 变量） */
const PALETTE = ['#3b82f6', '#22c55e', '#f59e0b', '#8b5cf6', '#06b6d4', '#ef4444']
const LABEL = { color: '#64748b', fontSize: 10 }
const AXIS_LINE = { lineStyle: { color: '#e2e8f0' } }
const SPLIT = { lineStyle: { color: '#eef2f6', type: 'dashed' as const } }

/** 给后端 option 套上密集化视觉：不改数据，只改外观 */
function densify(opt: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...opt, color: PALETTE }

  const raw = Array.isArray(opt.series) ? (opt.series as Record<string, unknown>[]) : []
  out.series = raw.map((s) => {
    const pts = Array.isArray(s.data) ? s.data.length : 0
    if (s.type === 'bar') {
      return {
        ...s,
        barMaxWidth: 26,
        itemStyle: { borderRadius: [2, 2, 0, 0] },
        emphasis: { itemStyle: { color: '#2563eb' } },
      }
    }
    if (s.type === 'line') {
      return { ...s, symbol: 'circle', symbolSize: 5, lineWidth: 2, showSymbol: pts <= 40 }
    }
    if (s.type === 'pie') {
      return {
        ...s,
        radius: ['46%', '68%'],
        itemStyle: { borderColor: '#fff', borderWidth: 1 },
        label: { fontSize: 10, color: '#64748b', formatter: '{b} {d}%' },
        labelLine: { length: 8, length2: 6 },
      }
    }
    return s
  })

  for (const key of ['xAxis', 'yAxis']) {
    const ax = opt[key]
    if (!ax) continue
    const axes = Array.isArray(ax) ? ax : [ax]
    out[key] = (axes as Record<string, unknown>[]).map((a) => ({
      ...a,
      axisLine: AXIS_LINE,
      axisTick: { show: false },
      axisLabel: { ...LABEL, ...(a.axisLabel as object) },
      splitLine: a.type === 'value' ? SPLIT : { show: false },
    }))
  }

  /* 网格贴边 + containLabel：标签不再被裁掉，画布利用率更高 */
  if (out.grid === undefined && (opt.xAxis || opt.yAxis)) {
    out.grid = { left: 4, right: 10, top: 26, bottom: 2, containLabel: true }
  }
  out.tooltip = {
    ...(opt.tooltip as object),
    backgroundColor: '#1e293b',
    borderWidth: 0,
    padding: [5, 8],
    textStyle: { color: '#fff', fontSize: 11 },
    extraCssText: 'border-radius:4px;',
  }
  if (opt.title) {
    out.title = {
      ...(opt.title as object),
      top: 2,
      left: 'center',
      textStyle: { fontSize: 11, fontWeight: 500, color: '#64748b' },
    }
  }
  out.animationDuration = 420
  out.animationEasing = 'cubicOut'
  return out
}

interface Props {
  option: Record<string, unknown>
  height?: number
}

export function EChart({ option, height = 260 }: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    if (!hostRef.current) return
    const chart = echarts.init(hostRef.current, undefined, { renderer: 'canvas' })
    chartRef.current = chart
    // 容器宽度随左栏展开/收起变化 → 用 ResizeObserver 跟随，而不是只监听 window
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(hostRef.current)
    return () => {
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    // notMerge：切换图表类型/换一轮数据时，不残留上一轮的 series
    chartRef.current?.setOption(densify(option ?? {}), { notMerge: true })
  }, [option])

  return <div className="echart" ref={hostRef} style={{ height }} />
}
