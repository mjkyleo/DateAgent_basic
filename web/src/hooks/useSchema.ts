/* 库表浏览器：数据库 → 表 → 字段，全部懒加载（展开了才请求）。
   采样 / 统计走后端有界接口（≤200 行），不自己 SELECT *。 */
import { useCallback, useEffect, useState } from 'react'
import { metaApi } from '../api'
import { errMessage } from './useAsync'
import type { ColumnMeta, SampleResult, TableStats } from '../api/types'

export function useSchemaBrowser(dsId: number | null) {
  const [databases, setDatabases] = useState<string[]>([])
  const [loadingDbs, setLoadingDbs] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [tables, setTables] = useState<Record<string, string[]>>({})
  const [loadingTables, setLoadingTables] = useState<Record<string, boolean>>({})
  const [openDb, setOpenDb] = useState<string | null>(null)
  const [openTable, setOpenTable] = useState<string | null>(null)

  const [columns, setColumns] = useState<ColumnMeta[]>([])
  const [loadingCols, setLoadingCols] = useState(false)
  const [sample, setSample] = useState<SampleResult | null>(null)
  const [stats, setStats] = useState<TableStats | null>(null)
  const [detailErr, setDetailErr] = useState<string | null>(null)

  /* 数据源变了就整体重置：表清单与字段都属于旧数据源 */
  useEffect(() => {
    setDatabases([]); setTables({}); setOpenDb(null); setOpenTable(null)
    setColumns([]); setSample(null); setStats(null); setError(null)
    if (!dsId && dsId !== 0) return
    let alive = true
    setLoadingDbs(true)
    metaApi.databases(dsId)
      .then((r) => { if (alive) setDatabases(r.databases) })
      .catch((e: unknown) => { if (alive) setError(errMessage(e)) })
      .finally(() => { if (alive) setLoadingDbs(false) })
    return () => { alive = false }
  }, [dsId])

  const toggleDb = useCallback((db: string, id: number | null) => {
    const next = openDb === db ? null : db
    setOpenDb(next)
    setOpenTable(null); setColumns([]); setSample(null); setStats(null); setDetailErr(null)
    if (!next || tables[next]) return
    setLoadingTables((p) => ({ ...p, [next]: true }))
    metaApi.tables(next, id)
      .then((r) => setTables((p) => ({ ...p, [next]: r.tables })))
      .catch((e: unknown) => setError(errMessage(e)))
      .finally(() => setLoadingTables((p) => ({ ...p, [next]: false })))
  }, [openDb, tables])

  const openTableDetail = useCallback(async (db: string, table: string, id: number | null) => {
    const next = openTable === table ? null : table
    setOpenTable(next)
    setColumns([]); setSample(null); setStats(null); setDetailErr(null)
    if (!next) return
    setLoadingCols(true)
    try {
      const schema = await metaApi.schema(db, next, id)
      setColumns(schema.columns)
    } catch (e: unknown) {
      setDetailErr(errMessage(e))
    } finally {
      setLoadingCols(false)
    }
  }, [openTable])

  const showSample = useCallback(async (db: string, table: string, id: number | null) => {
    setStats(null); setDetailErr(null)
    try {
      setSample(await metaApi.sample(db, table, 10, id))
    } catch (e: unknown) { setDetailErr(errMessage(e)) }
  }, [])

  const showStats = useCallback(async (db: string, table: string, id: number | null) => {
    setSample(null); setDetailErr(null)
    try {
      setStats(await metaApi.stats(db, table, id))
    } catch (e: unknown) { setDetailErr(errMessage(e)) }
  }, [])

  return {
    databases, loadingDbs, error, tables, loadingTables, openDb, toggleDb,
    openTable, openTableDetail, columns, loadingCols,
    sample, stats, detailErr, showSample, showStats,
  }
}
