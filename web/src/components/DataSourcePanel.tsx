/* 数据源面板（底部弹出）：清单 + 激活切换 + 测试连接 + 编辑 / 删除 + 新增 MySQL 连接。
   文件导入在输入区（上传后自动建源并激活），这里只管已有数据源。 */
import { useState } from 'react'
import { useApp, useTestConnection } from '../state/AppProvider'
import { datasourceApi } from '../api'
import type { Datasource, DatasourceCreate } from '../api/types'
import { errMessage, useAction } from '../hooks/useAsync'
import { AsyncView, Chip } from './Primitives'

export function DataSourcePanel() {
  const app = useApp()
  const test = useTestConnection()
  const [editing, setEditing] = useState<Datasource | null>(null)
  const remove = useAction(app.removeDs)
  const switchDs = useAction(app.switchDs)

  async function onRemove(ds: Datasource) {
    if (!confirm(`删除数据源「${ds.name}」？${ds.type === 'file' ? '（连同导入的数据文件）' : ''}`)) return
    await remove.run(ds.id)
    app.rounds.reload()
  }

  return (
    <div className="ds">
      <AsyncView loading={app.datasources.loading} error={app.datasources.error}
        isEmpty={(app.datasources.data?.length ?? 0) === 0}
        empty={<p className="panel-hint">还没有数据源：新增 MySQL 连接，或用输入区的「导入文件」。</p>}>
        <ul className="ds-list">
          {(app.datasources.data ?? []).map((ds) => (
            <li key={ds.id} className={`ds-item ${ds.is_active ? 'on' : ''}`}>
              <div className="ds-main">
                <button className="ds-name" onClick={() => void switchDs.run(ds.id)}
                        title="点击切换为激活数据源">
                  {ds.name}
                  {ds.is_active && <Chip tone="ok">激活</Chip>}
                  <Chip tone="neutral">{ds.type === 'file' ? '文件' : 'MySQL'}</Chip>
                </button>
                <span className="ds-sub">
                  {ds.type === 'file'
                    ? `表 ${ds.database || '—'}`
                    : `${ds.host}:${ds.port}/${ds.database || '—'}`}
                </span>
                {test.results[ds.id] && (
                  <span className={`ds-test ${test.results[ds.id].ok ? 'ok' : 'bad'}`}>
                    {test.results[ds.id].msg}
                  </span>
                )}
              </div>
              <div className="ds-actions">
                <button className="btn-ghost btn-xs" onClick={() => void test.run(ds.id)}
                        disabled={test.pendingId === ds.id}>
                  {test.pendingId === ds.id ? '测试中…' : '测试'}
                </button>
                {ds.type === 'mysql' && (
                  <button className="btn-ghost btn-xs" onClick={() => setEditing(ds)}>编辑</button>
                )}
                <button className="btn-ghost btn-xs btn-danger"
                        onClick={() => void onRemove(ds)}>删除</button>
              </div>
            </li>
          ))}
        </ul>
      </AsyncView>

      {remove.error && <p className="note note-bad">{errMessage(remove.error)}</p>}
      {switchDs.error && <p className="note note-bad">{switchDs.error}</p>}

      <div className="ds-form-wrap">
        <div className="ds-form-head">
          <b>{editing ? `编辑数据源「${editing.name}」` : '新增 MySQL 连接'}</b>
          {editing && <button className="btn-ghost btn-xs" onClick={() => setEditing(null)}>取消编辑</button>}
        </div>
        <DataSourceForm
          key={editing?.id ?? 'new'}
          editing={editing}
          onDone={() => { setEditing(null); app.datasources.reload() }}
        />
      </div>
    </div>
  )
}

function DataSourceForm({ editing, onDone }: { editing: Datasource | null; onDone: () => void }) {
  const [form, setForm] = useState<DatasourceCreate>({
    name: editing?.name ?? '',
    type: 'mysql',
    host: editing?.host ?? '127.0.0.1',
    port: editing?.port ?? 3306,
    user: editing?.user ?? '',
    password: '',                        // 后端永不回显密码；留空 = 保持原密码
    database: editing?.database ?? '',
  })
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const set = <K extends keyof DatasourceCreate>(k: K, v: DatasourceCreate[K]) =>
    setForm((p) => ({ ...p, [k]: v }))

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setPending(true); setError(null)
    try {
      if (editing) await datasourceApi.update(editing.id, form)
      else await datasourceApi.create(form)
      onDone()
    } catch (err: unknown) {
      setError(errMessage(err))
    } finally {
      setPending(false)
    }
  }

  return (
    <form className="ds-form" onSubmit={(e) => void submit(e)}>
      <label>
        <span>名称</span>
        <input value={form.name} onChange={(e) => set('name', e.target.value)}
               placeholder="如：本地订单库" required maxLength={64} />
      </label>
      <div className="ds-form-row">
        <label>
          <span>主机</span>
          <input value={form.host} onChange={(e) => set('host', e.target.value)} required />
        </label>
        <label className="narrow">
          <span>端口</span>
          <input type="number" value={form.port} onChange={(e) => set('port', Number(e.target.value))} />
        </label>
      </div>
      <div className="ds-form-row">
        <label>
          <span>用户名</span>
          <input value={form.user} onChange={(e) => set('user', e.target.value)} />
        </label>
        <label>
          <span>密码{editing ? '（留空保持不变）' : ''}</span>
          <input type="password" autoComplete="current-password" value={form.password}
                 onChange={(e) => set('password', e.target.value)} />
        </label>
      </div>
      <label>
        <span>默认数据库</span>
        <input value={form.database} onChange={(e) => set('database', e.target.value)}
               placeholder="可留空，提问时再选" />
      </label>
      <div className="form-actions">
        <button className="btn-primary" type="submit" disabled={pending}>
          {pending ? '保存中…' : editing ? '保存修改' : '新增连接'}
        </button>
      </div>
      {error && <p className="note note-bad">{error}</p>}
      <p className="note">后端只保存连接参数，密码不回显；所有查询强制只读。</p>
    </form>
  )
}
