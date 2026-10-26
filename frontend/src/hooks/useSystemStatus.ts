import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { healthApi, type SelfCheckItem, type SystemStatusSnapshot } from '@panwatch/api'
import { usePausedInterval } from '@panwatch/base-ui'
import { emptySystemStatus, readSystemStatusCache, saveSystemStatusCache, withProbeResults } from '@/lib/system-status'

export const SystemStatusContext = createContext<ReturnType<typeof useLocalSystemStatus> | null>(null)
export function useSystemStatus(enabled = true) {
  const shared = useContext(SystemStatusContext)
  const local = useLocalSystemStatus(enabled && !shared)
  return shared ?? local
}
export function useLocalSystemStatus(enabled = true) {
  const [snapshot, setSnapshot] = useState<SystemStatusSnapshot>(() => readSystemStatusCache() ?? emptySystemStatus())
  const snapshotRef = useRef(snapshot)
  snapshotRef.current = snapshot
  const [pendingNodes, setPendingNodes] = useState<string[]>(() => snapshot.nodes.map(node => node.id))
  const [refreshProgress, setRefreshProgress] = useState({ done: 0, total: 9 })
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState('')
  const [checking, setChecking] = useState(false)
  const [checks, setChecks] = useState<SelfCheckItem[]>([])
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [checkedAt, setCheckedAt] = useState<number | null>(null)
  const [visible, setVisible] = useState(document.visibilityState !== 'hidden')
  const alive = useRef(true)
  const streamRef = useRef<AbortController | null>(null)
  const generation = useRef(0)
  useEffect(() => { alive.current = true; const stream = streamRef; return () => { alive.current = false; const request = stream.current; stream.current = null; request?.abort('paused') } }, [])
  useEffect(() => {
    const changed = () => setVisible(document.visibilityState !== 'hidden')
    document.addEventListener('visibilitychange', changed)
    return () => document.removeEventListener('visibilitychange', changed)
  }, [])
  useEffect(() => { if (!enabled) { generation.current++; setChecking(false) }
    if (!enabled || !visible) { streamRef.current?.abort('paused'); streamRef.current = null; setLoading(false) }
  }, [enabled, visible])
  useEffect(() => {
    if (checkedAt === null) return
    const timer = window.setTimeout(() => setChecks([]), 60_000)
    return () => window.clearTimeout(timer)
  }, [checkedAt])
  const refresh = useCallback(async () => {
    if (!enabled || streamRef.current) return
    const controller = new AbortController()
    streamRef.current = controller
    const current = () => alive.current && streamRef.current === controller
    let assembled = { ...snapshotRef.current, nodes: snapshotRef.current.nodes.map(node => ({ ...node, connected: false })), edges: snapshotRef.current.edges.map(edge => ({ ...edge, connected: false })) }
    const received = new Set<string>()
    setSnapshot(assembled); setPendingNodes(assembled.nodes.map(node => node.id)); setRefreshProgress({ done: 0, total: assembled.nodes.length })
    setLoading(true); setError('')
    const timer = window.setTimeout(() => controller.abort('timeout'), 15_000)
    try {
      await healthApi.streamSystemStatus(update => {
        if (!current()) return
        const incoming = new Map(update.nodes.filter(node => assembled.nodes.some(existing => existing.id === node.id)).map(node => [node.id, node]))
        for (const id of incoming.keys()) received.add(id)
        const nodes = assembled.nodes.map(node => incoming.get(node.id) ?? node)
        const connected = new Map(nodes.map(node => [node.id, node.connected]))
        assembled = { ...assembled, nodes, version: update.version ?? assembled.version,
          checked_at: update.done ? update.checked_at : assembled.checked_at,
          edges: assembled.edges.map(edge => ({ ...edge, connected: !!connected.get(edge.source) && !!connected.get(edge.target) })) }
        setSnapshot(assembled)
        setPendingNodes(nodes.filter(node => !received.has(node.id)).map(node => node.id))
        setRefreshProgress({ done: received.size, total: nodes.length })
        if (update.done) {
          if (received.size !== nodes.length) throw new Error('模块状态未全部更新，保留历史记录')
          saveSystemStatusCache(assembled)
        }
      }, controller.signal)
    } catch (cause) {
      if (current() && controller.signal.reason !== 'paused') setError(controller.signal.reason === 'timeout' ? '状态更新超时，保留已取得的数据' : cause instanceof Error ? cause.message : '状态读取失败')
    } finally {
      window.clearTimeout(timer)
      if (current()) { streamRef.current = null; setLoading(false) }
    }
  }, [enabled])
  usePausedInterval(() => { void refresh() }, enabled && visible ? 30_000 : 0)
  const runChecks = useCallback(async (notifySend = false) => {
    if (!enabled) return
    const run = ++generation.current
    const current = () => alive.current && run === generation.current
    setChecking(true); setChecks([]); setCheckedAt(null); setProgress({ done: 0, total: 0 }); setError('')
    try {
      const list = await healthApi.selfcheckList()
      if (!current()) return
      const items = list.items ?? []
      setProgress({ done: 0, total: items.length })
      let cursor = 0
      const worker = async () => {
        while (current() && cursor < items.length) {
          const item = items[cursor++]
          let result: SelfCheckItem
          try {
            const response = await healthApi.selfcheckKeys([item.key], notifySend)
            const found = response.items?.find(row => row.key === item.key)
            if (!found) throw new Error('未返回检测结果')
            result = found
          } catch (cause) {
            result = { ...item, category: item.category as SelfCheckItem['category'], status: 'fail', latency_ms: 0, error: cause instanceof Error ? cause.message : '请求失败', hint: '检测请求失败，请重试', note: null }
          }
          if (!current()) return
          setChecks(previous => [...previous, result])
          setProgress(previous => ({ ...previous, done: previous.done + 1 }))
        }
      }
      await Promise.all(Array.from({ length: Math.min(4, items.length) }, worker))
      if (current()) setCheckedAt(Date.now())
    } catch (cause) { if (current()) setError(cause instanceof Error ? cause.message : '自检失败') }
    finally { if (current()) setChecking(false) }
  }, [enabled])
  // 主动探测只在 60 秒内点亮；长时间未验证不冒充持续在线。
  const currentChecks = checking || (checkedAt !== null && Date.now() - checkedAt < 60_000) ? checks : []
  const data = withProbeResults(snapshot, currentChecks)
  if (data && checking) {
    for (const node of data.nodes) {
      if (['datasource', 'ai', 'notify'].includes(node.id)) { node.status = 'checking'; node.connected = false }
    }
    const connected = new Map(data.nodes.map(node => [node.id, node.connected]))
    data.edges = data.edges.map(edge => ({ ...edge, connected: !!connected.get(edge.source) && !!connected.get(edge.target) }))
  }
  return { data, loading, pendingNodes, refreshProgress, error, refresh, checking, runChecks, checks, progress, checkedAt }
}
