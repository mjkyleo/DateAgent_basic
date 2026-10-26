/* 阶段①的「数据源洞察」：智能体对所选数据源的系统性描述与分析。

   内容分四层（对应后端 source_insight 的分层）：
   1. 概览 —— LLM 结合业务场景的描述（无 LLM 时回落规则摘要）；
   2. 数据里的现象 —— 每条都带一个真实聚合出来的数字；
   3. 表级画像 —— 每张表的行数、索引、业务角色，以及组织方式问题；
   4. 改进计划 —— LLM 按"影响面 × 成本"排序的优化项 + 查询建议。

/* 规则层结论（索引/主键/类型选型）来自 information_schema 实取，不依赖 LLM；
   LLM 只负责把这些事实讲成业务语言。

   取数分两段（对应后端 narrate 参数）：先拿 1~2 秒的规则层事实把面板铺开，
   再在后台单独取业务化叙述 —— 那份 JSON 实测要 150 秒以上才生成完，
   让第一步干等它，用户看到的就是一个转圈两分钟、最后还可能判失败的界面。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { agentApi } from '../../api'
import type { SourceInsightResult, Task } from '../../api/types'
import { errMessage } from '../../hooks/useAsync'
import { useApp } from '../../state/AppProvider'
import { Chip, CountUp, ErrorNote, Kpi, Skeleton } from '../Primitives'
import { AdviceList } from './AdviceItem'
import { ModelIcon, PlusIcon, RefreshIcon } from '../icons'

export function SourceInsightPanel() {
  const app = useApp()
  const [data, setData] = useState<SourceInsightResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  /* 已加入清单的改进项（按序号记）：按钮要当场变成"已在清单"，
     否则用户不知道点过没有，会重复加一样的任务 */
  const [planned, setPlanned] = useState<Record<number, boolean>>({})
  const [addingPlan, setAddingPlan] = useState<number | null>(null)
  /* 后台叙述层的状态：生成中 / 失败原因（失败不打断事实展示，只给一个重试入口） */
  const [narrating, setNarrating] = useState(false)
  const [narrErr, setNarrErr] = useState('')
  /* 只对"最后一次取数"的叙述结果负责：重新剖析时旧的慢响应可能后到，
     把已经作废的一版事实盖回去 */
  const seq = useRef(0)

  /* 改进计划 → 当前项目的任务清单：priority 进标题前缀，顺序一眼可辨 */
  async function addPlanItem(p: SourceInsightResult['optimization_plan'][number], i: number) {
    setAddingPlan(i)
    try {
      const title = `P${p.priority || i + 1} ${p.title}`
      const exists = (app.tasks.data ?? []).some((t: Task) => t.title === title)
      if (!exists) {
        await app.addTask({
          title,
          note: [p.why, p.how && `做法：${p.how}`].filter(Boolean).join('　'),
          source_kind: 'advice',
          ds_id: app.activeDs?.id ?? 0,
          source_ref: { db: data?.database ?? '', priority: p.priority || i + 1 },
        })
      }
      setPlanned((prev) => ({ ...prev, [i]: true }))
    } finally {
      setAddingPlan(null)
    }
  }

  /* 业务化叙述：慢（实测 150 秒以上），只在后台跑，成则合并进事实、败则留重试入口。
     只覆盖叙述相关字段 —— 表级画像与问题清单以先渲染的那份为准，不让两次剖析结果混在一列里。 */
  const loadNarration = useCallback(async (refresh: boolean, mySeq: number) => {
    if (!app.activeDs) return
    setNarrating(true)
    setNarrErr('')
    try {
      const r = await agentApi.sourceInsight({
        ds_id: app.activeDs.id,
        db: app.selection.db || undefined,
        refresh,
        narrate: true,
      })
      if (mySeq !== seq.current) return          // 已经有更新的一版，丢掉
      setData((prev) => (prev ? {
        ...prev,
        overview: r.overview, phenomena: r.phenomena, table_roles: r.table_roles,
        optimization_plan: r.optimization_plan, query_tips: r.query_tips,
        llm_used: r.llm_used, model_label: r.model_label, note: r.note,
      } : r))
    } catch (e: unknown) {
      if (mySeq === seq.current) setNarrErr(errMessage(e))
    } finally {
      if (mySeq === seq.current) setNarrating(false)
    }
  }, [app.activeDs, app.selection.db])

  const load = useCallback(async (refresh = false) => {
    if (!app.activeDs) return
    const mySeq = ++seq.current
    setLoading(true)
    setError('')
    try {
      const r = await agentApi.sourceInsight({
        ds_id: app.activeDs.id,
        db: app.selection.db || undefined,
        refresh,
        narrate: false,
      })
      if (mySeq !== seq.current) return
      setData(r)
      /* 事实先落地就开始后台补叙述，界面不等它 */
      if (!r.llm_used) void loadNarration(refresh, mySeq)
    } catch (e: unknown) {
      if (mySeq === seq.current) setError(errMessage(e))
    } finally {
      if (mySeq === seq.current) setLoading(false)
    }
  }, [app.activeDs, app.selection.db, loadNarration])

  /* 首次进入解析意图阶段就剖析一次（后端有缓存，重复进入不重复花钱） */
  useEffect(() => {
    if (!data && !loading && !error) void load(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (!app.activeDs) {
    return <p className="panel-hint">先激活一个数据源，才能对它做系统性剖析。</p>
  }

  if (loading && !data) {
    return (
      <div className="insight-panel">
        <div className="kpi-row">
          <Kpi label="剖析表数" value={<Skeleton w={28} />} />
          <Kpi label="总行数估计" value={<Skeleton w={52} />} />
          <Kpi label="发现问题" value={<Skeleton w={28} />} />
        </div>
        <Skeleton h={44} />
        <Skeleton h={28} />
        <Skeleton h={28} />
      </div>
    )
  }

  if (error && !data) return <ErrorNote>{error}</ErrorNote>
  if (!data) return null

  return (
    <div className="insight-panel">
      <div className="insight-toolbar">
        <span className="insight-src">
          剖析对象：<code>{data.database}</code>
          {data.from_cache && <Chip tone="neutral">缓存</Chip>}
          {data.llm_used
            ? <Chip tone="accent">大模型解读 · {data.model_label}</Chip>
            : narrating
              ? <Chip tone="neutral">业务化叙述生成中…</Chip>
              : <Chip tone="warn">规则引擎</Chip>}
        </span>
        <span className="insight-acts">
          {!data.llm_used && !narrating && (
            <button className="btn-ghost btn-xs" disabled={loading}
                    onClick={() => void loadNarration(false, seq.current)}
                    title="让大模型把这些事实讲成业务场景（要几十秒到两分钟）">
              <ModelIcon />{narrErr ? '重试 AI 叙述' : 'AI 叙述'}
            </button>
          )}
          <button className="btn-ghost btn-xs" disabled={loading}
                  onClick={() => void load(true)} title="强制重新剖析（绕过缓存）">
            <RefreshIcon />重新剖析
          </button>
        </span>
      </div>

      {narrErr && !data.llm_used && (
        <p className="note note-warn">业务化叙述没成功（{narrErr}），
          下面这些事实与结论不依赖大模型，照常可用。</p>
      )}

      <div className="kpi-row">
        <Kpi label="剖析表数" value={<CountUp value={data.table_count} />} />
        <Kpi label="总行数估计" value={<CountUp value={data.total_row_estimate} />} />
        <Kpi label="组织问题" value={<CountUp value={data.advices.length} />}
             tone={data.advices.some((a) => a.severity === 'high') ? 'bad' : undefined} />
      </div>

      {data.overview && <p className="insight-overview">{data.overview}</p>}
      {data.note && <p className="note note-warn">{data.note}</p>}

      {data.phenomena.length > 0 && (
        <div className="insight-block">
          <div className="insight-block-head">
            <b>数据里的现象</b>
            <span className="result-sub">每条都指回一次真实聚合的结果</span>
          </div>
          <ul className="tip-list">
            {data.phenomena.map((t, i) => <li key={i}>{t}</li>)}
          </ul>
        </div>
      )}

      {data.optimization_plan.length > 0 && (
        <div className="insight-block">
          <div className="insight-block-head">
            <b>改进计划</b>
            <span className="result-sub">按影响面 × 实施成本排序</span>
          </div>
          <ol className="plan-list stagger">
            {data.optimization_plan.slice(0, 6).map((p, i) => (
              <li key={i} className="plan-item">
                <span className="plan-no">{p.priority || i + 1}</span>
                <div className="plan-main">
                  <b>{p.title}</b>
                  {p.why && <p className="plan-why">{p.why}</p>}
                  {p.how && <p className="plan-how">做法：{p.how}</p>}
                </div>
                <button className={`plan-add ${planned[i] ? 'is-done' : ''}`}
                        disabled={Boolean(planned[i]) || addingPlan !== null}
                        onClick={() => void addPlanItem(p, i)}
                        title="加入当前项目的任务清单：可调序、可核对后统一执行">
                  <PlusIcon />{planned[i] ? '已在清单' : addingPlan === i ? '加入中…' : '加入清单'}
                </button>
              </li>
            ))}
          </ol>
        </div>
      )}

      {data.query_tips.length > 0 && (
        <div className="insight-block">
          <div className="insight-block-head"><b>查询建议</b></div>
          <ul className="tip-list">
            {data.query_tips.map((t, i) => <li key={i}>{t}</li>)}
          </ul>
        </div>
      )}

      {data.advices.length > 0 && (
        <div className="insight-block">
          <div className="insight-block-head">
            <b>数据组织方式问题</b>
            <span className="result-sub">{data.advices.length} 条 · 附依据</span>
          </div>
          <AdviceList advices={data.advices}
                      ctx={{ db: data.database, dsId: app.activeDs?.id }} />
        </div>
      )}

      <details className="insight-tables">
        <summary>表级画像（{data.tables.length} 张）</summary>
        <ul className="table-profile-list">
          {data.tables.map((t) => (
            <li key={t.table} className="table-profile">
              <div className="tp-head">
                <code className="tp-name">{t.table}</code>
                <span className="tp-rows">{t.row_estimate?.toLocaleString() ?? '—'} 行</span>
                <Chip tone={t.indexes.length ? 'ok' : 'warn'}>
                  {t.indexes.length ? `${t.indexes.length} 个索引` : '无索引'}
                </Chip>
              </div>
              {t.role && <p className="tp-role">{t.role}</p>}
              {t.indexes.length > 0 && (
                <p className="tp-idx">
                  索引：{t.indexes.map((i) => `${i.name}(${(i.columns || []).join(', ')})`).join('；')}
                </p>
              )}
              {t.advices.length > 0 && (
                <AdviceList advices={t.advices}
                            ctx={{ db: data.database, table: t.table, dsId: app.activeDs?.id }} />
              )}
            </li>
          ))}
        </ul>
      </details>
    </div>
  )
}
