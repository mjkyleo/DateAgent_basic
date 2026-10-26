/* SQL 代码块。
   - SqlBlock：只读展示。排版走 lib/sqlFormat 做子句级分行缩进，关键字着色，
     并提供「复制原文」与「复制排版」两个出口（粘贴到客户端执行用原文更稳，
     写文档用排版版更好读）。
   - SqlEditor：编辑态。等宽 textarea，可一键预览排版效果。 */
import { useMemo, useState } from 'react'
import { formatSql } from '../lib/sqlFormat'
import { CopyIcon } from './icons'

const KEYWORDS = /\b(SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|LIMIT|JOIN|LEFT|RIGHT|INNER|ON|AS|AND|OR|NOT|IN|IS|NULL|DESC|ASC|COUNT|SUM|AVG|MIN|MAX|CASE|WHEN|THEN|ELSE|END|HAVING|DISTINCT|BETWEEN|LIKE|DATE_FORMAT|YEAR|MONTH|DAY)\b/gi

/** 着色用的占位哨兵（不可见字符，正常 SQL 里不会出现） */
const MARK = String.fromCharCode(1)

const escapeHtml = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

/** 先转义再着色：字符串字面量先占位，避免关键字正则误伤 'select' 这类词 */
export function highlightSql(sql: string): string {
  const escaped = escapeHtml(sql)
  const stash: string[] = []
  const masked = escaped.replace(/'[^']*'/g, (m) => {
    stash.push(`<span class="sql-str">${m}</span>`)
    return `${MARK}${stash.length - 1}${MARK}`
  })
  const colored = masked.replace(KEYWORDS, (m) => `<span class="sql-kw">${m}</span>`)
  const restore = new RegExp(`${MARK}(\\d+)${MARK}`, 'g')
  return colored.replace(restore, (_, i: string) => stash[Number(i)])
}

interface BlockProps {
  sql: string
  title?: string
  note?: string          // 例如「本次执行使用手工修改版」
  tone?: 'default' | 'warn'
}

export function SqlBlock({ sql, title, note, tone = 'default' }: BlockProps) {
  const pretty = useMemo(() => formatSql(sql), [sql])
  const [copied, setCopied] = useState<'raw' | 'pretty' | null>(null)

  async function copy(text: string, tag: 'raw' | 'pretty') {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(tag)
      setTimeout(() => setCopied(null), 1600)
    } catch { /* 剪贴板不可用时静默 */ }
  }

  return (
    <div className={`code-block ${tone === 'warn' ? 'is-warn' : ''}`}>
      <div className="code-head">
        <span>{title || 'SQL'}</span>
        <span className="code-actions">
          <button className="btn-ghost btn-xs" onClick={() => void copy(sql, 'raw')}>
            <CopyIcon />{copied === 'raw' ? '已复制' : '复制原文'}
          </button>
          <button className="btn-ghost btn-xs" onClick={() => void copy(pretty, 'pretty')}>
            <CopyIcon />{copied === 'pretty' ? '已复制' : '复制排版'}
          </button>
        </span>
      </div>
      <pre><code dangerouslySetInnerHTML={{ __html: highlightSql(pretty) }} /></pre>
      {note && <p className="code-note">{note}</p>}
    </div>
  )
}

interface EditorProps {
  value: string
  onChange: (v: string) => void
  title?: string
  hint?: string
}

export function SqlEditor({ value, onChange, title, hint }: EditorProps) {
  const [preview, setPreview] = useState(false)

  return (
    <div className="code-block is-edit">
      <div className="code-head">
        <span>{title || '编辑 SQL'}</span>
        <span className="code-actions">
          <button className={`btn-ghost btn-xs ${preview ? 'on' : ''}`}
                  onClick={() => setPreview((v) => !v)}>
            {preview ? '继续编辑' : '预览排版'}
          </button>
        </span>
      </div>
      {preview
        ? <pre><code dangerouslySetInnerHTML={{ __html: highlightSql(formatSql(value)) }} /></pre>
        : <textarea className="sql-input" value={value} spellCheck={false}
                    rows={Math.min(14, Math.max(4, value.split('\n').length))}
                    onChange={(e) => onChange(e.target.value)} />}
      {hint && <p className="code-note">{hint}</p>}
    </div>
  )
}
