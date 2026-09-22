import { useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api'

/**
 * 批量收盘价(2026-09-22) —— 给列表行的 sparkline 用。
 *
 * 为什么要有这个 hook: 列表行要画"近 20 日走势"(40×16), 但**每行一次 /klines/{symbol}**
 * 在持仓 73 行时就是 73 个请求 —— 页面会被自己拖死。后端为此新开了轻量端点
 * `GET /api/klines/closes`(只读 PG、只要收盘价、一次最多 60 只)。
 *
 * 诚实口径:
 * - 后端没给数据的标的, 这里**就是没有**(`undefined`) ⇒ `<Sparkline>` 自己返回 null 不画,
 *   绝不补 0 / 不画假平线;
 * - 请求失败 ⇒ 全空(不重试到页面卡住), 列表照常显示数字(退化只影响走势图这一列);
 * - 代码集变了才重取; 超过 60 只自动**分批**(后端有上限, 这里先切好, 不打无谓的 400)。
 */
export interface BatchClosesResult {
  /** symbol → 收盘价序列(未命中 = 无该键) */
  closes: Record<string, number[]>
  loading: boolean
  /** 这次没拿到的标的(后端如实回的 missing) */
  missing: string[]
}

const MAX_PER_REQUEST = 60

export function useBatchCloses(symbols: string[], days = 20, enabled = true): BatchClosesResult {
  const [closes, setCloses] = useState<Record<string, number[]>>({})
  const [missing, setMissing] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  // 用"排序去重后的代码串"当依赖 —— 数组字面量每次渲染都是新引用, 直接进依赖会无限重取
  const key = Array.from(new Set(symbols.filter(Boolean))).sort().join(',')
  const keyRef = useRef('')

  useEffect(() => {
    if (!enabled || !key) {
      setCloses({})
      setMissing([])
      return
    }
    if (keyRef.current === key) return
    keyRef.current = key
    let alive = true
    const list = key.split(',')
    const batches: string[][] = []
    for (let i = 0; i < list.length; i += MAX_PER_REQUEST) batches.push(list.slice(i, i + MAX_PER_REQUEST))
    setLoading(true)
    void (async () => {
      const merged: Record<string, number[]> = {}
      const miss: string[] = []
      for (const b of batches) {
        try {
          const res = await fetchAPI<{ items?: { symbol: string; closes: number[] }[]; missing?: string[] }>(
            `/klines/closes?symbols=${encodeURIComponent(b.join(','))}&days=${days}`,
          )
          for (const it of res?.items ?? []) if (Array.isArray(it.closes)) merged[it.symbol] = it.closes
          miss.push(...(res?.missing ?? []))
        } catch {
          // 该批失败: 这几个标的就没有 sparkline(不编造, 不阻塞列表其它内容)
          miss.push(...b)
        }
      }
      if (!alive) return
      setCloses(merged)
      setMissing(miss)
      setLoading(false)
    })()
    return () => {
      alive = false
    }
  }, [key, days, enabled])

  return { closes, loading, missing }
}

export default useBatchCloses
