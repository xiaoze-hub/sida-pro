import { useEffect, useState } from 'react'

import { datasourcesApi, type VendorTrustItem } from '@panwatch/api'

/**
 * vendor 质量分订阅 (C1, 2026-09-10): 默认 60s 轮询 /datasources/trust。
 *
 * 供顶部心跳条与数据源页"行情源质量"共用。诚实口径: 请求失败置 error 标记
 * (条上显式提示), 保留上一次快照不装没事; 从未有样本 → items 空, 由调用方决定展示。
 */
export interface VendorTrustState {
  items: VendorTrustItem[]
  loading: boolean
  /** 最近一次请求是否失败(失败时 items 为上一次快照, 供显式标注) */
  error: boolean
}

const POLL_MS = 60_000

export function useVendorTrust(pollMs: number = POLL_MS): VendorTrustState {
  const [items, setItems] = useState<VendorTrustItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  useEffect(() => {
    let alive = true

    const load = async () => {
      try {
        const d = await datasourcesApi.trust<{ items?: VendorTrustItem[] }>()
        if (!alive) return
        setItems(Array.isArray(d?.items) ? d.items : [])
        setError(false)
      } catch {
        // 失败不覆盖快照(旧读数带"刷新失败"标注仍比空白有信息), 但绝不静默
        if (alive) setError(true)
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

  return { items, loading, error }
}
