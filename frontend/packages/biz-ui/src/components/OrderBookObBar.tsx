import { useCallback, useEffect, useRef, useState } from 'react'
import { Activity, RefreshCw } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'

/**
 * OB 盘口失衡条(2026-08-20)
 * 挂在 DarkFlowCards 底部: 展示订单簿失衡(买|卖比例横条)。
 * 数据来自 GET /api/orderbook-ob?symbol=XXX(fetchAPI 自动补 /api 前缀, 这里传 /orderbook-ob)。
 * - 买压/卖压/中性判定: 最新快照 OB = (买前10档金额 - 卖前10档金额)/(两者之和) ∈ [-1, 1]
 *   > +0.3 → 买压(红) / < -0.3 → 卖压(绿) / 其余中性
 * - 横条比例 = 最新快照 bid_amt10(买) : ask_amt10(卖)
 * - available:false / 请求失败 / 解析异常 → 渲染 null(静默降级, 不打扰主卡片)
 * - 加载中轻量骨架; 30s 自动刷新, 与 DarkFlowCards 节奏一致。
 */

export interface OrderBookObRow {
  ts: number
  dt: string
  bid_amt10: number | null
  ask_amt10: number | null
  ob: number
  label: string
}

export interface OrderBookObResp {
  available: boolean
  ob_series: OrderBookObRow[]
  events: Array<Record<string, unknown>>
  ghost_ratio: number
  note?: string | null
}

export default function OrderBookObBar({ symbol }: { symbol: string }) {
  const [data, setData] = useState<OrderBookObResp | null>(null)
  const [loading, setLoading] = useState(true)
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    if (!symbol) return
    try {
      const res = await fetchAPI<OrderBookObResp>(`/orderbook-ob?symbol=${encodeURIComponent(symbol)}`, {
        cacheMode: 'reload', // 盘中实时, 跳过 GET 缓存
      })
      if (mountedRef.current) setData(res)
    } catch {
      // 失败静默: 置空 → 外层渲染 null
      if (mountedRef.current) setData(null)
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [symbol])

  useEffect(() => {
    mountedRef.current = true
    setLoading(true)
    void load()
    const timer = window.setInterval(() => void load(), 30000) // 与 DarkFlowCards 30s 轮询一致
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [load])

  // 加载中轻量骨架(首次加载、尚无数据时)
  if (loading && !data) {
    return (
      <div className="rounded-xl border border-border/50 bg-card p-3">
        <div className="flex items-center gap-1.5 mb-2">
          <Activity className="w-3.5 h-3.5 text-muted-foreground" />
          <span className="text-[13px] font-semibold text-foreground">OB 盘口失衡</span>
        </div>
        <div className="h-2 rounded-full bg-accent/40 animate-pulse" />
      </div>
    )
  }

  // 不可用 / 请求失败 / 无序列 → 渲染 null(静默降级)
  if (!data || !data.available || !data.ob_series?.length) return null

  const latest = data.ob_series[data.ob_series.length - 1]
  const ob = latest.ob ?? 0
  const bidAmt = latest.bid_amt10 ?? 0
  const askAmt = latest.ask_amt10 ?? 0
  const total = bidAmt + askAmt
  const buyPct = total > 0 ? (bidAmt / total) * 100 : 50
  const sellPct = total > 0 ? (askAmt / total) * 100 : 50
  const tag = ob >= 0.3 ? '买压' : ob <= -0.3 ? '卖压' : '中性'
  const tagClass = ob >= 0.3 ? 'text-rose-400' : ob <= -0.3 ? 'text-emerald-400' : 'text-muted-foreground'

  return (
    <div className="rounded-xl border border-border/50 bg-card p-3">
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="text-[13px] font-semibold text-foreground flex items-center gap-1.5">
          <Activity className="w-3.5 h-3.5 text-muted-foreground" />
          OB 盘口失衡
        </div>
        <button
          type="button"
          title="刷新"
          onClick={() => {
            setLoading(true)
            void load()
          }}
          className="text-muted-foreground hover:text-foreground transition-colors"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* 买|卖比例横条: 买(红)左 / 卖(绿)右 */}
      <div className="flex items-center gap-2 text-[11px] mb-1.5">
        <span className="text-rose-400 font-mono shrink-0">买 {buyPct.toFixed(1)}%</span>
        <div className="flex-1 h-2 rounded-full bg-accent/40 overflow-hidden flex">
          <div className="h-full bg-rose-400/80 transition-all" style={{ width: `${buyPct}%` }} />
          <div className="h-full bg-emerald-400/80 transition-all" style={{ width: `${sellPct}%` }} />
        </div>
        <span className="text-emerald-400 font-mono shrink-0">卖 {sellPct.toFixed(1)}%</span>
      </div>

      <div className="flex items-center justify-between text-[11px] text-muted-foreground">
        <span>
          OB <span className={`font-mono font-medium ${tagClass}`}>{ob >= 0 ? '+' : ''}{ob.toFixed(3)}</span>
          <span className={`ml-1.5 font-medium ${tagClass}`}>{tag}</span>
        </span>
        <span>幽灵单比率 {(data.ghost_ratio * 100).toFixed(1)}% · 事件 {data.events.length} 条</span>
      </div>

      {data.note ? (
        <div className="mt-1.5 text-[11px] text-muted-foreground truncate" title={data.note}>
          {data.note}
        </div>
      ) : null}
    </div>
  )
}