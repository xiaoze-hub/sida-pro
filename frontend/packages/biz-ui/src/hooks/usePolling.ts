import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * 统一轮询 hook (2026-08-21)
 * 抽象 DarkFlowCards / MainFlowCompareCard 手写轮询的重复逻辑:
 *   - 首次加载 + intervalMs 定时刷新, 自动清理 timer / mounted ref。
 *   - 失败时保留上次成功数据(error 与 data 并存, UI 显示顶部细条警告而非整卡替换)。
 *   - loader 依赖变化(如 symbol 切换)时自动重置并重新拉取。
 */

export interface UsePollingOptions<T> {
  loader: () => Promise<T>
  /** 轮询间隔 ms, 不传 / 0 则只拉一次 */
  intervalMs?: number
  /** 首次是否立即拉取, 默认 true */
  immediate?: boolean
}

export interface UsePollingResult<T> {
  data: T | null
  loading: boolean
  /** 最近一次失败原因; 与 data 可并存(此时 data 为上次成功值) */
  error: unknown
  /** 手动刷新(置 loading 并重新拉取) */
  refresh: () => void
  /** 最近一次成功拉取的时间 */
  updatedAt: Date | null
}

export function usePolling<T>({
  loader,
  intervalMs,
  immediate = true,
}: UsePollingOptions<T>): UsePollingResult<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(immediate)
  const [error, setError] = useState<unknown>(null)
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)

  const timerRef = useRef<number | null>(null)

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }, [])

  const load = useCallback(async () => {
    try {
      const res = await loader()
      setData(res)
      setError(null)
      setUpdatedAt(new Date())
    } catch (e) {
      // 保留上次成功数据(error 与 data 并存)
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [loader])

  const refresh = useCallback(() => {
    setLoading(true)
    void load()
  }, [load])

  useEffect(() => {
    if (immediate) void load()
    if (intervalMs && intervalMs > 0) {
      timerRef.current = window.setInterval(() => void load(), intervalMs)
    }
    return () => {
      clearTimer()
    }
  }, [immediate, intervalMs, load, clearTimer])

  // 卸载兜底: 防止卸载后 setState 警告
  useEffect(() => clearTimer, [clearTimer])

  return { data, loading, error, updatedAt, refresh }
}
