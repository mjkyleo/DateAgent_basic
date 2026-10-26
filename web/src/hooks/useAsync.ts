import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react'
import { ApiError } from '../api/http'

export function errMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return '未知错误'
}

interface AsyncState<T> {
  data: T | null
  loading: boolean
  error: string | null
}

/* 数据请求的统一形态：自动拉一次 + 支持手动 reload。
   - alive 标志：组件卸载/依赖变化后不再 setState（避免"迟到响应覆盖新数据"）
   - reload 用内部 nonce 触发，不依赖调用方维护刷新标记 */
export function useAsync<T>(fn: () => Promise<T>, deps: DependencyList = []): AsyncState<T> & {
  reload: () => void
  setData: (updater: T | ((prev: T | null) => T | null)) => void
} {
  const [state, setState] = useState<AsyncState<T>>({ data: null, loading: true, error: null })
  const [nonce, setNonce] = useState(0)
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    let alive = true
    setState((prev) => ({ ...prev, loading: true, error: null }))
    fnRef.current()
      .then((data) => { if (alive) setState({ data, loading: false, error: null }) })
      .catch((e: unknown) => {
        if (alive) setState({ data: null, loading: false, error: errMessage(e) })
      })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  const reload = useCallback(() => setNonce((n) => n + 1), [])
  const setData = useCallback((updater: T | ((prev: T | null) => T | null)) => {
    setState((prev) => ({
      ...prev,
      data: typeof updater === 'function'
        ? (updater as (p: T | null) => T | null)(prev.data)
        : updater,
    }))
  }, [])

  return { ...state, reload, setData }
}

/* 一次性动作（提交/删除/切换）：自己管 pending 与错误，不参与自动拉取 */
export function useAction<A extends unknown[], R>(
  fn: (...args: A) => Promise<R>,
): { run: (...args: A) => Promise<R | undefined>; pending: boolean; error: string | null } {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fnRef = useRef(fn)
  fnRef.current = fn

  const run = useCallback(async (...args: A) => {
    setPending(true)
    setError(null)
    try {
      return await fnRef.current(...args)
    } catch (e: unknown) {
      setError(errMessage(e))
      return undefined
    } finally {
      setPending(false)
    }
  }, [])

  return { run, pending, error }
}
