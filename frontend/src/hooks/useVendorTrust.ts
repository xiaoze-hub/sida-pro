import { useCallback, useEffect, useRef, useState } from 'react'

import { datasourcesApi, type VendorTrustItem } from '@panwatch/api'
import { usePausedInterval } from '@panwatch/base-ui'

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

  // 卸载后不 setState(原 effect 的 `alive` 闭包改为 ref, 以配合 hook 化的轮询体)。
  const aliveRef = useRef(true)
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  const load = useCallback(async () => {
    try {
      const d = await datasourcesApi.trust<{ items?: VendorTrustItem[] }>()
      if (!aliveRef.current) return
      setItems(Array.isArray(d?.items) ? d.items : [])
      setError(false)
    } catch {
      // 失败不覆盖快照(旧读数带"刷新失败"标注仍比空白有信息), 但绝不静默
      if (aliveRef.current) setError(true)
    } finally {
      if (aliveRef.current) setLoading(false)
    }
  }, [])

  // 页签 keep-alive(2026-10-15): 轮询走 `usePausedInterval` —— 隐藏页签停表, 回前台补一次。
  usePausedInterval(load, pollMs, { deps: [pollMs] })

  return { items, loading, error }
}
