/* 模型供应商接入：快捷模板标签 + 表单 + 已接入列表。
   设计要点：
   1) 模板只回填表单，不直接落库 —— 落库必须有「测试连接 → 确认添加」这一步，
      否则用户会攒下一堆连不通的配置；
   2) 「接口地址」要含版本前缀（/v1、/api/paas/v4），协议只写 /chat/completions，
      请求 URL = 两者拼接。模板里已按各家实际路径预填；
   3) 密钥只写不读：编辑时密钥框留空 = 保持原密钥（后端同样约定）；
   4) 未填模型名时「测试连接」只验证端点与密钥，填了才验证该模型真能调用。 */
import { useMemo, useState } from 'react'
import { providersApi } from '../api'
import type { Provider, ProviderTemplate } from '../api/types'
import { errMessage, useAsync } from '../hooks/useAsync'
import { useApp, useTestProvider } from '../state/AppProvider'
import { Chip, ErrorNote } from './Primitives'
import { ConfirmDialog } from './ConfirmDialog'
import { CheckIcon, PlusIcon, RefreshIcon, TrashIcon } from './icons'

/* 各家 OpenAI 兼容根地址。接口地址含版本前缀，协议统一 /chat/completions。
   MiMo 的两个模板刻意留空地址：不确定的地址填进去，比空着更费时间排查。 */
const TEMPLATES: ProviderTemplate[] = [
  { key: 'custom', label: '自定义 / Custom', providerKey: '', name: '', apiBase: '', protocol: '/chat/completions' },
  { key: 'mimo-key', label: 'MiMo API Key', providerKey: 'mimo', name: 'MiMo', apiBase: '', protocol: '/chat/completions', needBase: true },
  { key: 'mimo-token', label: 'MiMo Token Plan', providerKey: 'mimo-token', name: 'MiMo Token Plan', apiBase: '', protocol: '/chat/completions', needBase: true },
  { key: 'deepseek', label: 'DeepSeek', providerKey: 'deepseek', name: 'DeepSeek', apiBase: 'https://api.deepseek.com/v1', protocol: '/chat/completions' },
  { key: 'kimi', label: 'Kimi', providerKey: 'kimi', name: 'Kimi', apiBase: 'https://api.moonshot.cn/v1', protocol: '/chat/completions' },
  { key: 'glm', label: '智谱 GLM', providerKey: 'glm', name: '智谱 GLM', apiBase: 'https://open.bigmodel.cn/api/paas/v4', protocol: '/chat/completions' },
  { key: 'qwen', label: '千问', providerKey: 'qwen', name: '千问', apiBase: 'https://dashscope.aliyuncs.com/compatible-mode/v1', protocol: '/chat/completions' },
  { key: 'minimax', label: 'MiniMax', providerKey: 'minimax', name: 'MiniMax', apiBase: 'https://api.minimaxi.com/v1', protocol: '/chat/completions' },
]

const PROTOCOLS = ['/chat/completions (OpenAI 兼容格式)']

interface Draft {
  provider_key: string
  name: string
  api_base: string
  api_key: string
  protocol: string
  model: string
}

const EMPTY: Draft = {
  provider_key: '', name: '', api_base: '', api_key: '',
  protocol: '/chat/completions', model: '',
}

export function ProviderPanel() {
  const app = useApp()
  const list = useAsync(() => providersApi.list(), [])
  const rows = list.data ?? []

  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [activeTpl, setActiveTpl] = useState('custom')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; msg: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [removing, setRemoving] = useState<Provider | null>(null)
  const [modelInputs, setModelInputs] = useState<Record<number, string>>({})
  const [reveal, setReveal] = useState(false)

  const tpl = useMemo(
    () => TEMPLATES.find((t) => t.key === activeTpl) ?? TEMPLATES[0],
    [activeTpl],
  )

  function pickTemplate(t: ProviderTemplate) {
    setActiveTpl(t.key)
    setTestResult(null)
    setErr(null)
    /* 自定义标签清空表单；其它标签回填该厂商默认值，密钥始终不动（用户在填） */
    setDraft((d) => ({
      ...EMPTY,
      api_key: d.api_key,
      provider_key: t.providerKey,
      name: t.name,
      api_base: t.apiBase,
      protocol: t.protocol,
    }))
  }

  const set = (k: keyof Draft) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setDraft((d) => ({ ...d, [k]: e.target.value }))

  async function testDraft() {
    setTesting(true)
    setErr(null)
    try {
      const r = await providersApi.testDraft({
        api_base: draft.api_base, api_key: draft.api_key,
        protocol: draft.protocol, model: draft.model,
      })
      setTestResult({ ok: r.connected, msg: r.msg })
    } catch (e: unknown) {
      setTestResult({ ok: false, msg: errMessage(e) })
    } finally {
      setTesting(false)
    }
  }

  async function submit() {
    setBusy(true)
    setErr(null)
    try {
      await providersApi.create({
        provider_key: draft.provider_key.trim(),
        name: draft.name.trim() || draft.provider_key.trim(),
        api_base: draft.api_base.trim(),
        api_key: draft.api_key,
        protocol: draft.protocol,
        models: draft.model.trim() ? [draft.model.trim()] : [],
      })
      setDraft((d) => ({ ...EMPTY, protocol: d.protocol }))
      setActiveTpl('custom')
      setTestResult(null)
      list.reload()
      app.providers.reload()
    } catch (e: unknown) {
      setErr(errMessage(e))
    } finally {
      setBusy(false)
    }
  }

  const canSubmit = Boolean(draft.provider_key.trim() && draft.name.trim()
    && draft.api_base.trim() && draft.api_key.trim())

  return (
    <div className="provider-panel">
      <div className="provider-toolbar">
        <button className="btn-ghost btn-xs" onClick={list.reload} title="刷新已接入列表">
          <RefreshIcon />刷新
        </button>
        <button className="btn-ghost btn-xs" onClick={() => pickTemplate(TEMPLATES[0])}
                title="清空表单，手动填写一家自定义供应商">
          <PlusIcon />添加供应商
        </button>
      </div>

      {/* 供应商快捷模板 */}
      <div className="tpl-tabs">
        {TEMPLATES.map((t) => (
          <button key={t.key}
                  className={`tpl-tab ${activeTpl === t.key ? 'on' : ''}`}
                  onClick={() => pickTemplate(t)}>
            {t.label}
          </button>
        ))}
      </div>

      {/* 配置表单 */}
      <div className="ds-form provider-form">
        <label>
          <span>供应商 ID</span>
          <input value={draft.provider_key} onChange={set('provider_key')}
                 placeholder="英文唯一标识，如 deepseek" />
        </label>
        <label>
          <span>名称</span>
          <input value={draft.name} onChange={set('name')} placeholder="展示名称，如 DeepSeek" />
        </label>
        <label>
          <span>接口地址</span>
          <input value={draft.api_base} onChange={set('api_base')}
                 placeholder={tpl.needBase ? '请填写：https://…/v1' : 'https://api.example.com/v1'} />
          <em className="field-hint">
            含版本前缀（如 /v1、/api/paas/v4），实际请求 = 接口地址 + 接口协议
          </em>
        </label>
        <label>
          <span>接口协议</span>
          <select value={draft.protocol} onChange={(e) => setDraft((d) => ({ ...d, protocol: e.target.value.split(' ')[0] }))}>
            {PROTOCOLS.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <label>
          <span>API Key</span>
          <span className="secret-input">
            <input type={reveal ? 'text' : 'password'} value={draft.api_key}
                   onChange={set('api_key')} placeholder="API Key" autoComplete="off" />
            <button type="button" className="reveal" onClick={() => setReveal((v) => !v)}
                    title={reveal ? '隐藏' : '显示明文'}>
              {reveal ? '隐藏' : '显示'}
            </button>
          </span>
        </label>
        <label>
          <span>默认模型名</span>
          <input value={draft.model} onChange={set('model')}
                 placeholder="可选，如 deepseek-chat" />
          <em className="field-hint">填了才能验证该模型是否真的可调用</em>
        </label>

        <div className="form-row">
          <button className="btn-ghost btn-sm" onClick={() => void testDraft()}
                  disabled={testing || !draft.api_base.trim() || !draft.api_key.trim()}>
            {testing ? '测试中…' : '测试连接'}
          </button>
          <button className="btn-primary btn-sm" onClick={() => void submit()}
                  disabled={busy || !canSubmit}>
            {busy ? '保存中…' : '确认添加'}
          </button>
        </div>

        {testResult && (
          <p className={`note ${testResult.ok ? 'note-ok' : 'note-bad'}`}>
            {testResult.ok ? '✓ ' : '✗ '}{testResult.msg}
          </p>
        )}
        {err && <ErrorNote>{err}</ErrorNote>}
      </div>

      {/* 已接入列表 */}
      <div className="provider-list-head">
        <b>已接入供应商</b>
        <span>{rows.length} 家</span>
      </div>
      {rows.length === 0 && (
        <p className="panel-hint">
          还没有接入任何供应商。选上面的模板填好配置，测试通过后点「确认添加」。
        </p>
      )}
      <ul className="provider-list stagger">
        {rows.map((p) => (
          <ProviderRow key={p.id} provider={p}
                       modelInput={modelInputs[p.id] ?? ''}
                       onModelInput={(v) => setModelInputs((s) => ({ ...s, [p.id]: v }))}
                       onChanged={() => { list.reload(); app.providers.reload() }}
                       onRemove={() => setRemoving(p)} />
        ))}
      </ul>

      <ConfirmDialog
        open={Boolean(removing)}
        title="确认删除该供应商？"
        target={removing ? `${removing.name}（${removing.provider_key}）` : undefined}
        hint="会同时删除它下面的模型清单。若删除的是当前使用中的模型，会自动切换到其余可用模型。"
        pending={busy}
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          if (!removing) return
          setBusy(true)
          providersApi.remove(removing.id)
            .then(() => { setRemoving(null); list.reload(); app.providers.reload() })
            .catch((e: unknown) => setErr(errMessage(e)))
            .finally(() => setBusy(false))
        }}
      />
    </div>
  )
}

function ProviderRow({ provider, modelInput, onModelInput, onChanged, onRemove }: {
  provider: Provider
  modelInput: string
  onModelInput: (v: string) => void
  onChanged: () => void
  onRemove: () => void
}) {
  const test = useTestProvider()
  const [err, setErr] = useState<string | null>(null)
  const result = test.results[provider.id]

  /* 拉取清单 → 勾选登记：免一个个手打模型名（大列表带过滤框） */
  const [picking, setPicking] = useState(false)
  const [remote, setRemote] = useState<string[] | null>(null)
  const [pulling, setPulling] = useState(false)
  const [filter, setFilter] = useState('')
  const [picked, setPicked] = useState<Set<string>>(new Set())

  async function toggleEnabled() {
    setErr(null)
    try {
      await providersApi.update(provider.id, { enabled: !provider.enabled })
      onChanged()
    } catch (e: unknown) {
      setErr(errMessage(e))
    }
  }

  async function addModel() {
    if (!modelInput.trim()) return
    setErr(null)
    try {
      await providersApi.addModel(provider.id, modelInput.trim())
      onModelInput('')
      onChanged()
    } catch (e: unknown) {
      setErr(errMessage(e))
    }
  }

  async function pullRemote() {
    if (picking) { setPicking(false); return }
    setPulling(true)
    setErr(null)
    try {
      const r = await providersApi.remoteModels(provider.id)
      if (!r.supported) {
        setErr(r.msg || '该端点不提供模型清单')
      } else {
        setRemote(r.models)
        setPicked(new Set())
        setFilter('')
        setPicking(true)
      }
    } catch (e: unknown) {
      setErr(errMessage(e))
    } finally {
      setPulling(false)
    }
  }

  async function registerPicked() {
    if (picked.size === 0) return
    setErr(null)
    try {
      await providersApi.addModelsBatch(provider.id, [...picked])
      setPicking(false)
      onChanged()
    } catch (e: unknown) {
      setErr(errMessage(e))
    }
  }

  const registered = new Set(provider.models.map((m) => m.model_name))
  const shown = (remote ?? []).filter((m) =>
    m.toLowerCase().includes(filter.trim().toLowerCase())).slice(0, 200)

  return (
    <li className={`provider-row ${provider.enabled ? '' : 'is-off'}`}>
      <div className="provider-row-head">
        <span className="provider-dot" />
        <b>{provider.name}</b>
        <code className="provider-key">{provider.provider_key}</code>
        {provider.enabled
          ? <Chip tone="ok">已启用</Chip>
          : <Chip tone="warn">已禁用</Chip>}
        {!provider.has_key && <Chip tone="bad">无密钥</Chip>}
        <span className="provider-actions">
          <button className="btn-ghost btn-xs" onClick={() => void toggleEnabled()}
                  title={provider.enabled ? '禁用后不再参与调用' : '启用'}>
            {provider.enabled ? '禁用' : '启用'}
          </button>
          <button className="btn-ghost btn-xs" onClick={() => void test.run(provider.id)}
                  disabled={test.pendingId === provider.id}
                  title="用该供应商的默认模型发一次极小对话">
            {test.pendingId === provider.id ? '测试中…' : '测试'}
          </button>
          <button className="btn-ghost btn-xs btn-danger" onClick={onRemove} title="删除供应商">
            <TrashIcon />
          </button>
        </span>
      </div>
      <div className="provider-row-meta">
        <code>{provider.api_base}{provider.protocol}</code>
      </div>
      <div className="provider-models">
        {provider.models.length === 0 && (
          <span className="panel-hint">还没有登记模型名，加一个才能被 Agent 选中</span>
        )}
        {provider.models.map((m) => (
          <span key={m.id} className={`model-chip ${m.is_active ? 'on' : ''}`}>
            <button className="model-chip-name" onClick={() => {
              void providersApi.activateModel(provider.id, m.id).then(onChanged)
            }} title={m.is_active ? '当前使用中' : '设为当前使用'}>
              {m.is_active && <CheckIcon />}
              <code>{m.model_name}</code>
            </button>
            <button className="model-chip-del" title="移除该模型"
                    onClick={() => void providersApi.removeModel(provider.id, m.id)
                      .then(onChanged).catch((e: unknown) => setErr(errMessage(e)))}>
              ×
            </button>
          </span>
        ))}
        <span className="model-add">
          <input value={modelInput} onChange={(e) => onModelInput(e.target.value)}
                 placeholder="模型名"
                 onKeyDown={(e) => { if (e.key === 'Enter') void addModel() }} />
          <button className="btn-ghost btn-xs" onClick={() => void addModel()}
                  disabled={!modelInput.trim()}>
            <PlusIcon />
          </button>
        </span>
        <button className="btn-ghost btn-xs" onClick={() => void pullRemote()}
                disabled={pulling}
                title="从供应商拉取模型清单，勾选后一键登记">
          {pulling ? '拉取中…' : picking ? '收起清单' : '拉取清单'}
        </button>
      </div>
      {picking && remote && (
        <div className="model-picker">
          <div className="model-picker-head">
            <input value={filter} onChange={(e) => setFilter(e.target.value)}
                   placeholder={`过滤 ${remote.length} 个模型…`} autoFocus />
            <button className="btn-primary btn-xs" onClick={() => void registerPicked()}
                    disabled={picked.size === 0}>
              登记所选（{picked.size}）
            </button>
          </div>
          <div className="model-picker-list">
            {shown.map((m) => {
              const exists = registered.has(m)
              const on = picked.has(m)
              return (
                <label key={m} className={`picker-item ${exists ? 'is-in' : ''}`}
                       title={exists ? '已登记' : '勾选后登记'}>
                  <input type="checkbox" disabled={exists} checked={exists || on}
                         onChange={() => setPicked((s) => {
                           const n = new Set(s)
                           if (n.has(m)) n.delete(m); else n.add(m)
                           return n
                         })} />
                  <code>{m}</code>
                </label>
              )
            })}
            {shown.length === 0 && <span className="panel-hint">没有匹配的模型</span>}
          </div>
        </div>
      )}
      {result && (
        <p className={`note ${result.ok ? 'note-ok' : 'note-bad'}`}>
          {result.ok ? '✓ ' : '✗ '}{result.msg}
        </p>
      )}
      {err && <ErrorNote>{err}</ErrorNote>}
    </li>
  )
}
