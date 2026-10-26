/* 分析结论：渐进式披露 —— KPI → 摘要 → 图表 → 明细 → 洞察 → SQL 溯源。
   数字全部来自本轮 SQL 的真实执行结果，文字解读为模板生成，故附 SQL 便于核对。
   Data Dense：顶部一排紧凑 KPI 卡（等宽数字，扫一眼就知道量级），
   每个区块独立淡入进场；图表类型与明细行数由外部（分析阶段的编辑区）持有，
   不在组件内起局部状态 —— 否则切换阶段再回来，参数就丢了。 */
import { useMemo } from 'react'
import type { AnalysisResult, ChartKind, QueryResult } from '../api/types'
import { ROW_LIMITS } from '../session/machine'
import { DataTable } from './DataTable'
import { EChart } from './EChart'
import { Chip, CountUp, Kpi } from './Primitives'
import { SqlBlock } from './CodeBlock'

export interface AnalysisPrefsView {
  chart: ChartKind
  rows: number
}

const CHART_OPTIONS: { value: ChartKind; label: string }[] = [
  { value: 'bar', label: '柱状图' },
  { value: 'line', label: '折线图' },
  { value: 'pie', label: '饼图' },
  { value: 'table', label: '仅表格' },
]

const INSIGHT_TONE = { anomaly: 'bad', trend: 'accent', comparison: 'ok' } as const
const INSIGHT_LABEL = { anomaly: '异常', trend: '趋势', comparison: '对比' } as const

/** 切换图表类型：后端给的是推荐类型的完整 option，这里只替换 series.type；
    饼图没有直角坐标系，需要去掉 xAxis/yAxis/grid 否则会出现空坐标轴。 */
function withChartType(option: Record<string, unknown>, type: ChartKind): Record<string, unknown> {
  const series = Array.isArray(option.series) ? option.series : []
  const next: Record<string, unknown> = {
    ...option,
    series: series.map((s) => ({ ...(s as Record<string, unknown>), type })),
  }
  if (type === 'pie') {
    delete next.xAxis
    delete next.yAxis
    delete next.grid
  }
  return next
}

interface Props {
  query: QueryResult
  analysis: AnalysisResult
  sql: string | null
  prefs: AnalysisPrefsView
  onPrefsChange: (patch: Partial<AnalysisPrefsView>) => void
}

export function ResultView({ query, analysis, sql, prefs, onPrefsChange }: Props) {
  const chartType = prefs.chart
  /* 只有真正带 series 才给图表区 —— 后端在无可用列时只回 {"title": ...}，
     这时画出来是个空坐标系，不如直接只展示表格。 */
  const hasChart = Array.isArray(analysis.chart_option?.series)
    && (analysis.chart_option.series as unknown[]).length > 0

  const option = useMemo(() => {
    if (!hasChart || chartType === 'table') return null
    return withChartType(analysis.chart_option, chartType)
  }, [analysis.chart_option, chartType, hasChart])

  return (
    <div className="result">
      {/* KPI 行：等宽数字 + 截断状态着色 */}
      <div className="kpi-row">
        <Kpi label="返回行数"
             value={<CountUp value={query.row_count} />}
             foot={query.truncated ? '已触达上限' : '完整结果'}
             tone={query.truncated ? 'bad' : 'ok'} />
        <Kpi label="字段数" value={<CountUp value={query.columns.length} />} />
        <Kpi label="查询耗时"
             value={<><CountUp value={query.cost_ms} /><small>ms</small></>} />
        <Kpi label="洞察条数" value={<CountUp value={analysis.insights.length} />}
             foot="规则引擎" />
      </div>

      <div className="result-block animate-in">
        <div className="result-head"><b>摘要</b></div>
        <p className="result-summary">{analysis.summary_text}</p>
        {query.truncated && (
          <p className="note note-warn">结果触达行数上限已截断，可追加条件缩小范围。</p>
        )}
      </div>

      {hasChart && (
        <div className="result-block animate-in">
          <div className="result-head">
            <b>图表</b>
            <div className="chart-switch">
              {CHART_OPTIONS.map((o) => (
                <button key={o.value}
                        className={`btn-ghost btn-xs ${chartType === o.value ? 'on' : ''}`}
                        onClick={() => onPrefsChange({ chart: o.value })}>
                  {o.label}
                </button>
              ))}
            </div>
          </div>
          {option
            ? <EChart option={option} />
            : <p className="panel-hint">已切换为「仅表格」</p>}
        </div>
      )}

      <div className="result-block animate-in">
        <div className="result-head">
          <b>明细数据</b>
          <div className="result-head-tools">
            <span className="row-limit">
              展示
              {ROW_LIMITS.map((n) => (
                <button key={n} className={`btn-ghost btn-xs ${prefs.rows === n ? 'on' : ''}`}
                        onClick={() => onPrefsChange({ rows: n })}>{n}</button>
              ))}
              行
            </span>
          </div>
        </div>
        <DataTable query={query} maxRows={prefs.rows} />
      </div>

      {analysis.insights.length > 0 && (
        <div className="result-block animate-in">
          <div className="result-head">
            <b>洞察</b>
            <span className="result-sub">{analysis.insights.length} 条 · 每条附依据</span>
          </div>
          <ul className="insight-list stagger">
            {analysis.insights.map((it, i) => (
              <li key={i} className={`insight insight-${it.type}`}>
                <div className="insight-head">
                  <Chip tone={INSIGHT_TONE[it.type]}>{INSIGHT_LABEL[it.type]}</Chip>
                  <b>{it.title}</b>
                </div>
                <p>{it.detail}</p>
                <p className="insight-evidence">依据：{it.evidence}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {sql && (
        <details className="result-sql">
          <summary>原始 SQL（数字溯源）</summary>
          <SqlBlock sql={sql} />
        </details>
      )}
    </div>
  )
}
