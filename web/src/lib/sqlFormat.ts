/* SQL 结构化排版。

   后端产出的是单行 SQL（generate_sql 追加 LIMIT 后原样返回），直接摊在一行里
   根本没法核对字段和条件。这里做子句级排版：
     SELECT / FROM / WHERE / GROUP BY / HAVING / ORDER BY / LIMIT / JOIN 各占一行；
     选中列逐列缩进一行；WHERE 内的 AND / OR 缩进后换行。

   三条硬约束（都是踩过才知道要守的）：
     1. 字符串字面量先占位：'%Y-%m'、'paid and shipped' 里的空格与关键字
        绝不能被当成语法结构切掉；
     2. 只在括号深度 0 处换行：DATE_FORMAT(x, '%m') 这类函数内部的逗号不动；
     3. 任何异常一律回落原文 —— 排版是锦上添花，绝不能把 SQL 显示错。 */

const INDENT = '    '

/** 字面量占位用的哨兵字符（不可见，正常 SQL 里不会出现） */
const MARK = String.fromCharCode(1)

/** 顶层子句：换行后回到 0 级缩进。长关键字在前，保证 UNION ALL 先于 UNION 命中 */
const CLAUSES = [
  'UNION ALL', 'UNION',
  'SELECT', 'FROM', 'WHERE', 'GROUP BY', 'HAVING', 'ORDER BY', 'LIMIT',
]

/** JOIN 与 FROM 同级，单独成行 */
const JOINS = ['LEFT JOIN', 'RIGHT JOIN', 'INNER JOIN', 'CROSS JOIN', 'FULL JOIN', 'JOIN']

/** 条件连接词：缩进一级后换行 */
const CONDS = ['AND', 'OR']

const ALL = [...CLAUSES, ...JOINS, ...CONDS]

interface Row { indent: number; text: string }

/** 在位置 i 尝试匹配关键字：要求左边界不是标识符字符，右边界不是单词字符 */
function matchKeyword(s: string, i: number): string | null {
  if (i > 0 && /[\w.$]/.test(s[i - 1])) return null
  const rest = s.slice(i)
  for (const kw of ALL) {
    const pattern = `^${kw.replace(/ /g, '\\s+')}(?![\\w])`
    const m = rest.match(new RegExp(pattern, 'i'))
    if (m) return m[0]
  }
  return null
}

/** 按顶层逗号切分（括号内的逗号属于函数参数，不切） */
function splitTopLevel(s: string): string[] {
  const out: string[] = []
  let depth = 0
  let cur = ''
  for (const ch of s) {
    if (ch === '(') depth += 1
    else if (ch === ')') depth -= 1
    if (ch === ',' && depth === 0) {
      out.push(cur.trim())
      cur = ''
      continue
    }
    cur += ch
  }
  if (cur.trim()) out.push(cur.trim())
  return out.filter(Boolean)
}

function tryFormat(sql: string): string {
  const flat = sql.replace(/\s+/g, ' ').trim()
  if (!flat) return sql

  // ① 字符串字面量占位（含 '' 转义写法）
  const literals: string[] = []
  const masked = flat.replace(/'(?:[^']|'')*'/g, (m) => {
    literals.push(m)
    return `${MARK}${literals.length - 1}${MARK}`
  })

  // ② 按顶层子句切成行
  const rows: Row[] = []
  let cur = ''
  let indent = 0
  let depth = 0
  let i = 0

  while (i < masked.length) {
    const ch = masked[i]
    if (ch === '(') { depth += 1; cur += ch; i += 1; continue }
    if (ch === ')') { depth -= 1; cur += ch; i += 1; continue }

    if (depth === 0) {
      const hit = matchKeyword(masked, i)
      if (hit) {
        if (cur.trim()) rows.push({ indent, text: cur.trim() })
        const upper = hit.toUpperCase().replace(/\s+/g, ' ')
        cur = upper
        indent = CONDS.includes(upper) ? 1 : 0
        i += hit.length
        continue
      }
    }
    cur += ch
    i += 1
  }
  if (cur.trim()) rows.push({ indent, text: cur.trim() })

  // ③ SELECT 列列表拆行：只在"列确实多/确实长"时才划算，
  //    单列 COUNT(*) 拆开反而更啰嗦
  const out: Row[] = []
  for (const row of rows) {
    if (/^SELECT\s/i.test(row.text)) {
      const rest = row.text.replace(/^SELECT\s+/i, '').trim()
      if (rest) {
        const cols = splitTopLevel(rest)
        if (cols.length > 1 || rest.length > 64) {
          out.push({ indent: 0, text: 'SELECT' })
          // 逗号跟在上一列行尾（splitTopLevel 切分时把它当作分隔符丢掉了，这里补回来）
          cols.forEach((c, i) => out.push({
            indent: 1,
            text: i < cols.length - 1 ? `${c},` : c,
          }))
          continue
        }
      }
    }
    out.push(row)
  }

  // ④ 还原字面量
  const restore = new RegExp(`${MARK}(\\d+)${MARK}`, 'g')
  return out
    .map((r) => INDENT.repeat(r.indent) + r.text)
    .join('\n')
    .replace(restore, (_, n: string) => literals[Number(n)] ?? '')
}

export function formatSql(sql: string): string {
  try {
    return tryFormat(sql)
  } catch {
    return sql
  }
}
