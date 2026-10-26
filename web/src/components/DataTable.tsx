/* 结果表格：高密度数据表。
   Data Dense 的三条规则在这里落地：
   1) 数值列 → 等宽 + 右对齐 + tabular-nums（列与列之间能竖向对齐扫描）；
   2) 状态列 → 彩色徽章（green=active / yellow=pending / red=error / blue=info），
      不读文字也能看出状态；
   3) 行高压到 py-1、斑马纹、sticky 表头，外加行号列方便指位。
   列类型只算一次（useMemo），不在每个单元格里重复判断。 */
import { useMemo } from 'react'
import type { QueryResult } from '../api/types'

export function cellText(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') {
    try { return JSON.stringify(v) } catch { return String(v) }
  }
  return String(v)
}

type Tone = 'ok' | 'pending' | 'bad' | 'info' | 'neutral'

/** 值 → 状态色。只做**整值精确匹配**（不子串匹配），且只收语义明确的词：
    状态词（active/pending/failed）、评价词（excellent/poor）、正负极（positive/negative）。
    刻意不收 high/low/good-ish 这类有歧义的词 —— 猜错颜色比不给颜色更糟。 */
const STATUS_RULES: { re: RegExp; tone: Tone }[] = [
  {
    re: /^(active|enabled|ok|success|succeeded|paid|done|completed|complete|approved|valid|online|true|yes|on|excellent|very good|good|positive|healthy|normal|正常|成功|已完成|已支付|有效|启用|是|优秀|良好|正面|健康)$/i,
    tone: 'ok',
  },
  {
    re: /^(pending|waiting|queued|processing|draft|average|fair|moderate|medium|待处理|等待|进行中|处理中|审核中|草稿|未支付|一般|中等|中)$/i,
    tone: 'pending',
  },
  {
    re: /^(error|failed|failure|cancelled|canceled|invalid|offline|false|no|off|poor|negative|critical|失败|异常|已取消|无效|禁用|否|差|负面|严重)$/i,
    tone: 'bad',
  },
  {
    re: /^(info|new|created|neutral|unknown|none|not rated|n\/a|信息|新建|已创建|中性|未知|未评价|无)$/i,
    tone: 'info',
  },
]

function statusTone(v: string): Tone {
  const hit = STATUS_RULES.find((r) => r.re.test(v.trim()))
  return hit ? hit.tone : 'neutral'
}

const isNumericValue = (v: unknown) =>
  v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v))

interface ColMeta {
  name: string
  numeric: boolean
  statusish: boolean
  idish: boolean
}

interface Props {
  query: QueryResult
  maxRows?: number
}

export function DataTable({ query, maxRows = 100 }: Props) {
  const rows = query.rows.slice(0, maxRows)

  const cols = useMemo<ColMeta[]>(() => {
    const present = (r: Record<string, unknown>, c: string) =>
      r[c] !== null && r[c] !== undefined && r[c] !== ''
    return query.columns.map((name) => {
      const values = query.rows.filter((r) => present(r, name)).map((r) => r[name])
      const distinct = new Set(values.map((v) => String(v)))
      /* 必须是"文本列"才可能当状态列：0/1 这类数值开关列走 mono 右对齐更诚实
         （把 1 涂成绿色、文字却显示 "1"，反而是噪声）。 */
      const anyText = values.some((v) => typeof v === 'string' && !isNumericValue(v))
      /* 状态列：列名像状态、取值很少、且多数取值能命中状态词表。
         三个条件同时满足才当状态列，避免把普通文本列误涂色。 */
      const statusish = anyText && values.length > 0
        && /(status|state|type|level|risk|flag|active|enabled|rating|quality|impact|priority|health|result|outcome|verdict|sentiment|状态|类型|等级|风险|评分|影响|结果|质量)/i.test(name)
        && distinct.size <= 10
        && values.filter((v) => statusTone(String(v)) !== 'neutral').length >= values.length * 0.6
      /* ID/编号列：等宽显示、左对齐（ID 是标识符不是度量，不该右对齐），
         且优先于"数值列"判定 —— 否则 6317637 这种 ID 会被当成金额右对齐。 */
      const idish = !statusish
        && /(^id$|_id$|^id_|code$|_code$|uuid|guid|_no$|^no$|sn|accn|accessionnumber)/i.test(name)
      const numeric = !statusish && !idish && values.length > 0 && values.every(isNumericValue)
      return { name, numeric, statusish, idish }
    })
  }, [query])

  return (
    <div className="table-wrap animate-in">
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th className="right" title="行号">#</th>
              {cols.map((c) => (
                <th key={c.name} className={c.numeric ? 'right' : ''} title={c.name}>{c.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                <td className="idx">{i + 1}</td>
                {cols.map((c) => {
                  const text = cellText(row[c.name])
                  if (c.statusish) {
                    return (
                      <td key={c.name} title={text}>
                        <span className={`cell-badge is-${statusTone(text)}`}>{text}</span>
                      </td>
                    )
                  }
                  return (
                    <td key={c.name} title={text}
                        className={c.numeric ? 'num' : c.idish ? 'mono' : ''}>
                      {text}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {/* 底栏：行数/列数/耗时/截断，替代原来散在表格下方的提示文字 */}
      <div className="table-foot">
        <span>显示 <b>{rows.length}</b> / <b>{query.row_count}</b> 行 · <b>{query.columns.length}</b> 列</span>
        <span>
          <b>{query.cost_ms}</b> ms{query.truncated ? ' · 已触达行数上限' : ''}
        </span>
      </div>
    </div>
  )
}
