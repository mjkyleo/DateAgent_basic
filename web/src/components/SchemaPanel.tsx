/* 库表结构面板（底部弹出）：库 → 表 → 字段，勾选的表/字段会作为提问上下文传给 parse_intent。
   每张表提供「采样 / 统计」快捷查看，走后端有界接口。 */
import { useApp } from '../state/AppProvider'
import { useSchemaBrowser } from '../hooks/useSchema'
import { AsyncView, Chip, Spinner } from './Primitives'
import { cellText } from './DataTable'

export function SchemaPanel() {
  const app = useApp()
  const dsId = app.activeDs?.id ?? null
  const s = useSchemaBrowser(dsId)
  const sel = app.selection

  function toggleTable(table: string) {
    const has = sel.tables.includes(table)
    app.setSelection({
      db: sel.db,
      tables: has ? sel.tables.filter((t) => t !== table) : [...sel.tables, table],
      fields: sel.fields,
    })
  }

  function toggleDb(db: string) {
    s.toggleDb(db, dsId)
    if (sel.db !== db) app.setSelection({ db, tables: [], fields: [] })
  }

  if (!app.activeDs) return <p className="panel-hint">请先激活一个数据源。</p>

  return (
    <div className="schema">
      <div className="schema-head">
        <AsyncView loading={s.loadingDbs} error={s.error} isEmpty={s.databases.length === 0}
          empty={<p className="panel-hint">该数据源下没有可见数据库。</p>}>
          <div className="schema-dbs">
            {s.databases.map((db) => (
              <button key={db}
                      className={`btn-ghost btn-sm ${s.openDb === db ? 'on' : ''}`}
                      onClick={() => toggleDb(db)}>
                {db}
              </button>
            ))}
          </div>
        </AsyncView>
        {sel.db && <span className="panel-hint">当前上下文库：{sel.db}</span>}
      </div>

      {s.openDb && (
        <div className="schema-body">
          {s.loadingTables[s.openDb] && <p className="panel-hint"><Spinner label="加载表清单…" /></p>}
          <ul className="tree">
            {(s.tables[s.openDb] ?? []).map((t) => (
              <li key={t} className="tree-item">
                <div className="tree-row">
                  <label className="tree-check">
                    <input type="checkbox" checked={sel.tables.includes(t)}
                           onChange={() => toggleTable(t)} />
                  </label>
                  <button className="tree-toggle" onClick={() => void s.openTableDetail(s.openDb!, t, dsId)}>
                    <i className={`caret ${s.openTable === t ? 'open' : ''}`} />
                    {t}
                  </button>
                  <span className="tree-actions">
                    <button className="btn-ghost btn-xs"
                            onClick={() => void s.showSample(s.openDb!, t, dsId)}>采样</button>
                    <button className="btn-ghost btn-xs"
                            onClick={() => void s.showStats(s.openDb!, t, dsId)}>统计</button>
                  </span>
                </div>

                {s.openTable === t && (
                  <div className="tree-detail">
                    {s.loadingCols && <p className="panel-hint"><Spinner label="加载字段…" /></p>}
                    {s.detailErr && <p className="note note-bad">{s.detailErr}</p>}
                    {s.columns.length > 0 && (
                      <ul className="field-list">
                        {s.columns.map((c) => (
                          <li key={c.name}>
                            <span className="field-name">{c.name}</span>
                            <span className="field-type">{c.type}</span>
                            {c.primary_key && <Chip tone="accent">PK</Chip>}
                          </li>
                        ))}
                      </ul>
                    )}
                    {s.sample && <SampleView rows={s.sample.rows} columns={Object.keys(s.sample.rows[0] ?? {})} />}
                    {s.stats && <StatsView stats={s.stats} />}
                  </div>
                )}
              </li>
            ))}
            {!s.loadingTables[s.openDb] && !(s.tables[s.openDb] ?? []).length && (
              <li className="panel-hint">该库下没有可访问的表。</li>
            )}
          </ul>
        </div>
      )}
    </div>
  )
}

function SampleView({ rows, columns }: { rows: Record<string, unknown>[]; columns: string[] }) {
  return (
    <div className="tree-block">
      <div className="tree-block-head">采样（≤10 行）</div>
      <div className="table-scroll">
        <table className="data-table compact">
          <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>{columns.map((c) => <td key={c}>{cellText(r[c])}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function StatsView({ stats }: { stats: import('../api/types').TableStats }) {
  return (
    <div className="tree-block">
      <div className="tree-block-head">分布统计 · 约 {stats.row_count} 行</div>
      <ul className="field-list">
        {stats.columns.map((c) => (
          <li key={c.column}>
            <span className="field-name">{c.column}</span>
            <span className="field-type">
              空值 {(c.null_ratio * 100).toFixed(0)}%
              {c.distinct_count !== null ? ` · 去重 ${c.distinct_count}` : ''}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
