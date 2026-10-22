import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import type { CopilotRunDetail, CopilotRunRow } from '@/lib/copilot/api'

/**
 * 参谋运行轨迹取数（骨架 + 单轮详情）。
 *
 * 职责边界：
 *  - 拉 `/copilot/runs`（可按 conversation_id 过滤）与 `/copilot/runs/{run_id}` 详情；
 *  - **缓存已加载的 detail**（Map）：同一轮重复展开不再打第二次请求；
 *  - 失败**静默**：返回 `{ok:false}`，不抛、不弹错误打断阅读（轨迹是旁路证据，
 *    拿不到就少一段说明，不能把整个对话页炸了）。
 *
 * 口径：接口失败/无记录一律空数组 + ok=false，**不补 0 假数据**。
 */

/** 单次请求的统一返回：成功给 data，失败给 ok=false（静默，不抛）。 */
export type SafeResult<T> = { ok: true; data: T } | { ok: false; error: string | null }

export interface UseCopilotRunsOptions {
  /** 只看某个会话的轮次；null/undefined = 不带该过滤条件 */
  conversationId?: number | null
  /** 拉取条数上限（后端 ≤100） */
  limit?: number
  /** 关闭后不发任何请求（页面没会话时用） */
  enabled?: boolean
}

export interface UseCopilotRunsResult {
  /** 最近一次请求是否成功（false = 静默失败，界面按"没有轨迹"处理） */
  ok: boolean
  runs: CopilotRunRow[]
  /** 已加载的单轮详情：run_id → 详情（缓存，重复展开零请求） */
  details: Record<string, CopilotRunDetail>
  loading: boolean
  /** 正在拉某轮详情 */
  detailLoading: boolean
  error: string | null
  /** 拉/复用某轮详情（已缓存则直接回填，不发请求） */
  loadDetail: (runId: string) => Promise<SafeResult<CopilotRunDetail>>
  /** 重新拉骨架 */
  reload: () => void
}

async function safeFetch<T>(path: string, signal?: AbortSignal): Promise<SafeResult<T>> {
  try {
    const data = await fetchAPI<T>(path, { signal })
    return { ok: true, data }
  } catch (e) {
    if (signal?.aborted) return { ok: false, error: null }
    const msg = e instanceof Error ? e.message : String(e)
    return { ok: false, error: msg || '请求失败' }
  }
}

export function useCopilotRuns(
  opts: UseCopilotRunsOptions = {},
): UseCopilotRunsResult {
  const { conversationId = null, limit = 20, enabled = true } = opts

  const [runs, setRuns] = useState<CopilotRunRow[]>([])
  const [details, setDetails] = useState<Record<string, CopilotRunDetail>>({})
  const [loading, setLoading] = useState(enabled)
  const [detailLoading, setDetailLoading] = useState(false)
  const [ok, setOk] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  /** detail 缓存：run_id → 详情。展开第二轮再折回来不再请求。 */
  const detailCache = useRef(new Map<string, CopilotRunDetail>())
  /** 在途详情请求：同一轮并发展开只发一次。 */
  const inflight = useRef(new Map<string, Promise<SafeResult<CopilotRunDetail>>>())
  /** 在途计数：detailLoading 由它驱动（并发时不被先完成的那个提前关掉）。 */
  const inflightCount = useRef(0)
  const abortRef = useRef<AbortController | null>(null)

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    const ac = new AbortController()
    abortRef.current = ac
    setLoading(true)
    const conv = conversationId === null || conversationId === undefined ? '' : `&conversation_id=${conversationId}`
    void safeFetch<CopilotRunRow[]>(`/copilot/runs?limit=${limit}${conv}`, ac.signal).then((res) => {
      if (ac.signal.aborted) return
      if (res.ok) {
        setRuns(Array.isArray(res.data) ? res.data : [])
        setOk(true)
        setError(null)
      } else {
        setRuns([])
        setOk(false)
        setError(res.error)
      }
      setLoading(false)
    })
    return () => ac.abort()
  }, [conversationId, limit, enabled, tick])

  const loadDetail = useCallback(
    async (runId: string): Promise<SafeResult<CopilotRunDetail>> => {
      const cached = detailCache.current.get(runId)
      if (cached) {
        setDetails((prev) => (prev[runId] ? prev : { ...prev, [runId]: cached }))
        return { ok: true, data: cached }
      }
      const pending = inflight.current.get(runId)
      if (pending) return pending

      setDetailLoading(true)
      inflightCount.current += 1
      const p = safeFetch<CopilotRunDetail>(
        `/copilot/runs/${encodeURIComponent(runId)}`,
      ).then((res) => {
        if (res.ok && res.data) {
          detailCache.current.set(runId, res.data)
          setDetails((prev) => ({ ...prev, [runId]: res.data }))
        }
        inflight.current.delete(runId)
        inflightCount.current -= 1
        if (inflightCount.current <= 0) setDetailLoading(false)
        return res
      })
      inflight.current.set(runId, p)
      return p
    },
    [],
  )

  const reload = useCallback(() => setTick((t) => t + 1), [])

  return {
    ok,
    runs,
    details,
    loading,
    detailLoading,
    error,
    loadDetail,
    reload,
  }
}

export default useCopilotRuns
