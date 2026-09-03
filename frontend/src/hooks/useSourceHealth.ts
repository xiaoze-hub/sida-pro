import { useEffect, useState } from 'react'

import { datasourcesApi } from '@panwatch/api'

/**
 * 设计稿 v2.1 §12: 数据源健康状态订阅。
 *
 * 前端每 60s 轮询一次 `/api/datasources/health`, 结果按逻辑源 id 索引,
 * 供 L4 事件图标决定**亮显 / 灰显 + tooltip**。
 *
 * 五个逻辑源(对应 §5.3 的 6 个图标):
 *   tck         → 拆 / ⚠撤
 *   img         → 🛡托 / 🔒压
 *   wencai      → 涨
 *   shadow      → 我
 *   tq_moreinfo → 明盘(TQ 扩展指标/决策先锋链路)
 *
 * ⚠️ 诚实口径: 请求失败 / 状态未知时一律按 **不可用** 处理(灰显),
 * 不假设"接口挂了但数据应该还在"。
 */
export type SourceStatus = 'connected' | 'degraded' | 'down' | 'unknown'

export interface SourceHealthItem {
  id: string
  name?: string
  status: SourceStatus
  last_check_at?: number
  detail?: string | null
  icons?: string[]
}

/** 事件图标 → 逻辑源 id(设计稿 §12 的 ICON_READY) */
export const ICON_SOURCE: Record<string, string> = {
  拆: 'tck',
  '⚠撤': 'tck',
  '🛡托': 'img',
  '🔒压': 'img',
  涨: 'wencai',
  我: 'shadow',
  '明盘': 'tq_moreinfo',
}

const POLL_MS = 60_000

export function useSourceHealth(pollMs: number = POLL_MS) {
  const [health, setHealth] = useState<Record<string, SourceHealthItem>>({})
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true

    const load = async () => {
      try {
        const data = await datasourcesApi.health<{ items?: SourceHealthItem[] }>()
        if (!alive) return
        const map: Record<string, SourceHealthItem> = {}
        for (const it of data?.items || []) map[it.id] = it
        setHealth(map)
      } catch {
        // 请求失败 → 保持空表, 上层按"不可用"灰显, 不伪造状态
        if (alive) setHealth({})
      } finally {
        if (alive) setLoading(false)
      }
    }

    void load()
    const timer = window.setInterval(load, pollMs)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [pollMs])

  /** 某个图标是否可用: 只有 connected 才算可用 */
  const isReady = (icon: string): boolean => {
    const src = ICON_SOURCE[icon]
    if (!src) return true // 未纳入健康检查的图标(如解套盘位/支撑压力)按 §5.3 走"不显示", 不灰显
    return health[src]?.status === 'connected'
  }

  /** 灰显 tooltip 文案 */
  const reasonOf = (icon: string): string => {
    const src = ICON_SOURCE[icon]
    if (!src) return ''
    const item = health[src]
    if (!item) return '数据源健康状态未知'
    if (item.status === 'connected') return ''
    const why = item.detail ? ` — ${item.detail}` : ''
    return `${item.name || src} 不可用(${item.status})${why}`
  }

  return { health, loading, isReady, reasonOf }
}
