import { useEffect, useRef, useState } from 'react'
import { fetchAPI } from '@panwatch/api'

/**
 * 竞价快览单行卡(2026-08-19)
 * 挂在 InteractiveKline 分时模式 DarkFlowCards 上方。数据来自
 * GET /api/auction-snapshot?symbol=XXX (fetchAPI 自动补 /api 前缀)。
 * 展示: 今日竞价 高开+3.38% | 撤单率 24.3% | 操作量 535.75万股 | 成交 5357.5万
 * available=false / 请求失败 -> 渲染 null(不占位不报错); 加载中显示暗色骨架。
 * 深色主题风格与 DarkFlowCards 一致(bg-card / border-border/50 / text-[11px])。
 */

export interface AuctionSnapshotData {
  available: boolean
  direction: string
  gap_pct: number | null
  withdraw_rate_full: number | null
  auction_price: number | null
  prev_close: number | null
  trade_volume: number | null
  note: string
}

/** 股数 -> 万股, 无数据显示 '--' */
function fmtWanShares(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v) || v <= 0) return '--'
  return `${(v / 10000).toFixed(2)}万股`
}

/** 成交金额(元) = 撮合量 × 竞价价, -> 万元, 无数据显示 '--' */
function fmtWanAmt(vol: number | null | undefined, price: number | null | undefined): string {
  if (vol == null || price == null || !Number.isFinite(vol) || !Number.isFinite(price) || vol <= 0) return '--'
  return `${((vol * price) / 10000).toFixed(1)}万`
}

function gapColor(direction: string): string {
  if (direction === '高开') return 'text-rose-400'
  if (direction === '低开') return 'text-emerald-400'
  return 'text-muted-foreground'
}

export default function AuctionSnapshotCard({ symbol, market }: { symbol: string; market: string }) {
  const [data, setData] = useState<AuctionSnapshotData | null>(null)
  const [loading, setLoading] = useState(true)
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    setLoading(true)
    setData(null)
    // 仅 A 股有竞价快照(thsdk tick_super_level1)
    if (!symbol || market !== 'CN') {
      setLoading(false)
      return
    }
    fetchAPI<AuctionSnapshotData>(`/auction-snapshot?symbol=${encodeURIComponent(symbol)}`, {
      cacheMode: 'reload',
    })
      .then((res) => {
        if (mountedRef.current) setData(res)
      })
      .catch(() => {
        if (mountedRef.current) setData(null)
      })
      .finally(() => {
        if (mountedRef.current) setLoading(false)
      })
    return () => {
      mountedRef.current = false
    }
  }, [symbol, market])

  // 加载中: 暗色骨架(与 DarkFlowCards 占位风格一致)
  if (loading) {
    return <div className="mt-3 h-[28px] rounded-xl border border-border/50 bg-card animate-pulse bg-accent/20" />
  }

  // 无数据 / 请求失败 / 后端 available=false -> 渲染 null
  if (!data || !data.available) return null

  const gapText =
    data.gap_pct != null && Number.isFinite(data.gap_pct)
      ? `${data.gap_pct > 0 ? '+' : ''}${data.gap_pct.toFixed(2)}%`
      : '--'
  const withdrawText =
    data.withdraw_rate_full != null && Number.isFinite(data.withdraw_rate_full)
      ? `${(data.withdraw_rate_full * 100).toFixed(1)}%`
      : '--'

  return (
    <div className="mt-3 flex items-center gap-3 overflow-x-auto whitespace-nowrap rounded-xl border border-border/50 bg-card px-3 py-1.5 text-[11px] text-muted-foreground">
      <span className="font-medium">今日竞价</span>
      <span className={gapColor(data.direction)}>
        {data.direction === '无数据' ? '无数据' : `${data.direction}${gapText}`}
      </span>
      <span className="opacity-40">|</span>
      <span>
        撤单率 <span className="text-foreground">{withdrawText}</span>
      </span>
      <span className="opacity-40">|</span>
      <span>
        操作量 <span className="text-foreground">{fmtWanShares(data.trade_volume)}</span>
      </span>
      <span className="opacity-40">|</span>
      <span>
        成交 <span className="text-foreground">{fmtWanAmt(data.trade_volume, data.auction_price)}</span>
      </span>
    </div>
  )
}
