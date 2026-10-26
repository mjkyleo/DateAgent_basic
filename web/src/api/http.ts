/* 请求层：统一 baseURL、超时、错误脱敏。
   —— 后端任何一层出错都返回 {"detail": "..."}，这里把它收敛成 ApiError，
      业务层只 catch 一种错误，不用在每个组件里判断 fetch 的各种失败形态。 */
import type { HealthResponse } from './types'

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

const DEFAULT_TIMEOUT_MS = 60_000   // 意图解析可能走 LLM，比普通接口宽松

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  timeoutMs?: number
  signal?: AbortSignal
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS, signal } = opts
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeoutMs)
  // 调用方信号与超时信号都要生效：任一触发即取消
  const onAbort = () => ctrl.abort()
  signal?.addEventListener('abort', onAbort)

  try {
    const res = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    })

    const text = await res.text()
    let payload: unknown = null
    if (text) {
      try { payload = JSON.parse(text) } catch { payload = text }
    }

    if (!res.ok) {
      // FastAPI 的 HTTPException / 兜底 handler 都是 {detail: string}；
      // 其它形态（网关 HTML、纯文本）给出带状态码的兜底文案
      const detail =
        payload && typeof payload === 'object' && 'detail' in payload
          ? String((payload as { detail: unknown }).detail)
          : typeof payload === 'string' && payload
            ? payload
            : `请求失败（HTTP ${res.status}）`
      throw new ApiError(detail, res.status)
    }
    return payload as T
  } catch (e) {
    if (e instanceof ApiError) throw e
    if (e instanceof DOMException && e.name === 'AbortError') {
      throw new ApiError(signal?.aborted ? '请求已取消' : '请求超时，请稍后重试', 0)
    }
    throw new ApiError(e instanceof Error ? e.message : '网络异常', 0)
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', onAbort)
  }
}

const get = <T>(path: string, opts?: RequestOptions) => request<T>(path, { ...opts, method: 'GET' })
const post = <T>(path: string, body?: unknown, opts?: RequestOptions) =>
  request<T>(path, { ...opts, method: 'POST', body })
const put = <T>(path: string, body?: unknown, opts?: RequestOptions) =>
  request<T>(path, { ...opts, method: 'PUT', body })
const patch = <T>(path: string, body?: unknown, opts?: RequestOptions) =>
  request<T>(path, { ...opts, method: 'PATCH', body })
const del = <T>(path: string, opts?: RequestOptions) => request<T>(path, { ...opts, method: 'DELETE' })

export const http = { get, post, put, patch, del, request }

/* 文件上传单独走 FormData：不能设 Content-Type，由浏览器自动带 boundary */
export async function upload<T>(path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(path, { method: 'POST', body: form })
  const text = await res.text()
  let payload: unknown = null
  if (text) { try { payload = JSON.parse(text) } catch { payload = text } }
  if (!res.ok) {
    const detail =
      payload && typeof payload === 'object' && 'detail' in payload
        ? String((payload as { detail: unknown }).detail)
        : `上传失败（HTTP ${res.status}）`
    throw new ApiError(detail, res.status)
  }
  return payload as T
}

export const healthApi = {
  get: () => http.get<HealthResponse>('/healthz', { timeoutMs: 8000 }),
}
