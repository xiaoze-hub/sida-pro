import { useCallback, useEffect, useRef, useState } from 'react'
import { getCopilotBrief } from '@/lib/copilot/api'
import type { CopilotBrief } from '@/lib/copilot/types'

type State = {
  data: CopilotBrief | null
  loading: boolean
  /** 事实性错误原文（后端 detail 或网络错误），前端不改写、不猜原因 */
  error: string | null
}

/**
 * 参谋卡取数：abort + 手动刷新 + 「过期快照」标记由后端给（前端不自己算）。
 *
 * 设计取舍：
 *  - `enabled=false` 时**不发请求**（指数/板块没有筹码与撤单证据，不浪费 8 个证据源）；
 *  - 超时 12s 主动 abort —— 全量证据里有 TQ/竞价源，慢是常态，但不能让卡片永远转圈；
 *  - 失败时保留旧 data（刷新失败不该把已有结论清空），只把 error 挂出来。
 */
export function useCopilotBrief(
  symbol: string,
  market = 'CN',
  opts?: { enabled?: boolean; timeoutMs?: number },
) {
  const enabled = opts?.enabled !== false
  const timeoutMs = opts?.timeoutMs ?? 12000
  const [state, setState] = useState<State>({ data: null, loading: enabled, error: null })
  const abortRef = useRef<AbortController | null>(null)

  const load = useCallback(
    async (refresh = false) => {
      if (!enabled || !symbol) return
      abortRef.current?.abort()
      const ac = new AbortController()
      abortRef.current = ac
      const timer = setTimeout(() => ac.abort(), timeoutMs)
      setState((s) => ({ ...s, loading: true, error: null }))
      try {
        const data = await getCopilotBrief(symbol, market, refresh, ac.signal)
        // 运行时形状校验：接口改了字段 / 网关返回了别的东西时，宁可显示"暂不可用"，
        // 也不能让 `data.verdict.intent_tone` 把整个工作台渲染树炸掉（UI 崩溃 > 少一张卡）。
        if (!data || typeof data !== 'object' || !data.verdict) {
          throw new Error('参谋接口返回格式异常（缺少 verdict）')
        }
        if (!ac.signal.aborted) setState({ data, loading: false, error: null })
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        if (ac.signal.aborted) {
          setState((s) => ({
            ...s,
            loading: false,
            error: s.data ? null : `请求超时（${Math.round(timeoutMs / 1000)}s）`,
          }))
        } else {
          setState((s) => ({ ...s, loading: false, error: msg || '请求失败' }))
        }
      } finally {
        clearTimeout(timer)
      }
    },
    [symbol, market, enabled, timeoutMs],
  )

  useEffect(() => {
    void load(false)
    return () => abortRef.current?.abort()
  }, [load])

  return { ...state, reload: () => load(true) }
}
