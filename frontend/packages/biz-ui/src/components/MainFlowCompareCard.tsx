import { useCallback, useEffect, useRef, useState } from 'react'
import { RefreshCw, AlertTriangle } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { HoverPopover } from '@panwatch/base-ui/components/ui/hover-popover'

/**
 * 主力意图双源对比卡(2026-08-20, v0.3.0)
 * 挂在 DarkFlowCards 内, 分时模式图表下方。
 * 数据 GET /api/main-flow/compare/{symbol} (fetchAPI 自动补 /api 前缀)。
 *
 * 口径:
 *   - tencent  腾讯逐笔口径(元): main_net(主力净额, ≥20万, 剔除竞价)、big_net(超大单 ≥100万)。
 *   - thsdk    同花顺 L2 口径(元): main_net(主买-主卖净额, 全量主动)、big_net(大单净额, ≥100万)。
 *   - consistency% (0-100) = 两源主力净额方向/量级一致性; delta_pct = 发散幅度% (=100-consistency)。
 * 一致性阈值: >90 绿, 60-90 蓝, <60 黄(警示)。<60 时提示双源差异大, 建议人工复核。
 * 60s 轮询(与暗盘/分时 30s 节奏错峰, 该接口后端自带 30s 进程内缓存)。
 */

export interface MainFlowSource {
  available: boolean
  data_status?: string | null
  main_net?: number | null // 元
  big_net?: number | null // 元
  mid_net?: number | null // 元(腾讯大单 20-100万)
  retail_net?: number | null // 元(腾讯散户)
  main_net_wan?: number | null // 万元(thsdk 原口径)
  main_buy_wan?: number | null
  main_sell_wan?: number | null
  tick_count?: number | null
  signal?: string | null
}

export interface MainFlowCompareResp {
  symbol: string
  tencent: MainFlowSource | null
  thsdk: MainFlowSource | null
  consistency: number | null // 0-100
  delta_pct: number | null // 发散幅度 %
  note?: string
}

/** 元 -> 亿/万 紧凑显示(带符号), NaN -> '--' */
function fmtYi(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--'
  const abs = Math.abs(v)
  const sign = v > 0 ? '+' : v < 0 ? '-' : ''
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)}亿`
  return `${sign}${(abs / 1e4).toFixed(0)}万`
}

function fmtSignedPct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '--'
  return `${v > 0 ? '+' : ''}${v.toFixed(1)}%`
}

function upColor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return 'text-muted-foreground'
  return v > 0 ? 'text-rose-600 dark:text-rose-400' : v < 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-muted-foreground'
}

/** 一致性分段配色: >90 绿 / 60-90 蓝 / <60 黄 */
function consistencyColor(consistency: number | null | undefined): { stroke: string; text: string; ring: string } {
  if (consistency == null || !Number.isFinite(consistency)) {
    return { stroke: '#64748b', text: 'text-slate-400', ring: 'border-slate-500/40' }
  }
  if (consistency > 90) return { stroke: '#10b981', text: 'text-emerald-500', ring: 'border-emerald-500/50' }
  if (consistency >= 60) return { stroke: '#22d3ee', text: 'text-cyan-500', ring: 'border-cyan-500/50' }
  return { stroke: '#eab308', text: 'text-amber-500', ring: 'border-amber-500/50' }
}

/** 圆形一致性进度条(纯 SVG, r=15, 周长 ≈94.2) */
function ConsistencyRing({ value }: { value: number | null | undefined }) {
  const clamped = value != null && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0
  const c = consistencyColor(value)
  const CIRC = 2 * Math.PI * 15 // ≈94.25
  const offset = CIRC * (1 - clamped / 100)
  return (
    <div className="relative w-10 h-10 shrink-0">
      <svg viewBox="0 0 40 40" className="w-10 h-10 -rotate-90">
        <circle cx="20" cy="20" r="15" fill="none" strokeWidth="4" className="stroke-white/10" />
        <circle
          cx="20"
          cy="20"
          r="15"
          fill="none"
          strokeWidth="4"
          strokeLinecap="round"
          stroke={c.stroke}
          strokeDasharray={CIRC}
          strokeDashoffset={offset}
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center">
        <span className={`font-mono tabular-nums text-[9px] ${c.text}`}>
          {value != null && Number.isFinite(value) ? `${Math.round(value)}` : '--'}
        </span>
      </div>
    </div>
  )
}

/** 单数据源列 */
function SourceCol({ label, tag, net }: { label: string; tag: string; net: number | null | undefined }) {
  return (
    <div className="rounded-lg bg-accent/20 px-2.5 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] text-muted-foreground">{label}</span>
        <span className="text-[9px] text-muted-foreground/70 border border-border/50 rounded px-1 py-px">{tag}</span>
      </div>
      <div className={`font-mono tabular-nums text-[13px] font-semibold mt-0.5 ${upColor(net)}`}>{fmtYi(net)}</div>
    </div>
  )
}

export default function MainFlowCompareCard({ symbol }: { symbol: string }) {
  const [data, setData] = useState<MainFlowCompareResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    if (!symbol) return
    setError('')
    try {
      const res = await fetchAPI<MainFlowCompareResp>(`/main-flow/compare/${encodeURIComponent(symbol)}`, {
        cacheMode: 'reload', // 盘中实时, 跳过 GET 缓存
      })
      if (mountedRef.current) setData(res)
      if (mountedRef.current) setUpdatedAt(new Date())
    } catch (e) {
      if (mountedRef.current) setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [symbol])

  useEffect(() => {
    mountedRef.current = true
    setLoading(true)
    void load()
    const timer = window.setInterval(() => void load(), 60000) // 60s 刷新
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [load])

  if (loading && !data) {
    return <div className="h-[96px] rounded-xl border border-border/50 bg-card animate-pulse bg-accent/20" />
  }

  const hasData = data?.tencent?.available || data?.thsdk?.available
  const tn = data?.tencent ?? null
  const th = data?.thsdk ?? null
  const consistency = data?.consistency ?? null
  const delta = data?.delta_pct ?? null
  const lowConsistency = consistency != null && Number.isFinite(consistency) && consistency < 60
  const cn = consistencyColor(consistency)

  if (!hasData) {
    return (
      <div className="rounded-xl border border-border/50 bg-card p-3">
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          {error || data?.note || '主力意图双源对比暂不可用(非A股/数据源不可用)'}
        </div>
      </div>
    )
  }

  return (
    <div className={`rounded-xl border bg-card p-3 ${lowConsistency ? 'border-amber-500/40' : 'border-border/50'}`}>
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="flex items-center gap-2">
          <div className="text-[13px] font-semibold text-foreground">主力意图双源对比</div>
          {updatedAt && (
            <span className="text-[10px] text-muted-foreground font-mono">
              {updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}
            </span>
          )}
        </div>
        <button
          type="button"
          title="刷新"
          onClick={() => { setLoading(true); void load() }}
          className="text-muted-foreground hover:text-foreground transition-colors"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <div className="flex items-center gap-3">
        {/* 一致性圆环 */}
        <HoverPopover
          trigger={
            <span className={`inline-flex items-center gap-1.5 rounded-lg border px-2 py-1.5 ${cn.ring}`}>
              <ConsistencyRing value={consistency} />
              <span className="leading-tight">
                <span className={`block text-[11px] font-semibold ${cn.text}`}>一致性 {fmtSignedPct(consistency)}</span>
                <span className="block text-[9px] text-muted-foreground">双源对比</span>
              </span>
            </span>
          }
          content={
            <div className="text-[11px] leading-relaxed max-w-[220px]">
              {lowConsistency
                ? '双源差异大, 建议人工复核'
                : '两数据源主力净额方向与量级的一致程度(thsdk L2 vs 腾讯逐笔)'}
              <div className="text-[10px] text-muted-foreground mt-1">delta {fmtSignedPct(delta)}</div>
            </div>
          }
          side="top"
          align="start"
        />

        {/* 两列净额 */}
        <div className="grid grid-cols-2 gap-2 flex-1">
          <SourceCol label="thsdk 主净额" tag="L2大单" net={th?.main_net} />
          <SourceCol label="腾讯 主净额" tag="逐笔" net={tn?.main_net} />
        </div>
      </div>

      {lowConsistency && (
        <div className="mt-2 flex items-center gap-1.5 text-[11px] text-amber-600 dark:text-amber-500">
          <AlertTriangle className="w-3.5 h-3.5" />
          差异幅度 delta {fmtSignedPct(delta)}, 建议人工复核
        </div>
      )}
      {data?.note ? <div className="mt-1.5 text-[10px] text-muted-foreground">{data.note}</div> : null}
    </div>
  )
}
