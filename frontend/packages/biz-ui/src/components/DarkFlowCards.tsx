import { useCallback, useEffect, useRef, useState } from 'react'
import { Bot, RefreshCw, ShieldAlert } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Button } from '@panwatch/base-ui/components/ui/button'
import OrderBookObBar from './OrderBookObBar'
import MainFlowCompareCard from './MainFlowCompareCard'
import DecisionPioneerCard from './DecisionPioneerCard'

/**
 * 主力意图 + 内盘外盘 双卡片(2026-08-13)
 * 挂在 InteractiveKline 分时模式图表下方。数据来自 GET /api/dark-flow?symbol=XXX
 * (fetchAPI 自动补 /api 前缀, 这里传 /dark-flow)。盘中每 30 秒自动刷新, 与分时图节奏一致。
 */

export interface DarkFlowMainIntent {
  main_net: number | null
  big_net: number | null
  mid_net: number | null
  retail_net: number | null
  main_intensity: number | null
  main_buy_ratio: number | null
  signal?: string | null
  data_status?: 'ok' | 'insufficient' | string | null
  /** 2026-09-04 P1-4: 结论翻转注记, 无翻转则 null(有值才展示) */
  verdict_note?: string | null
}

export interface DarkFlowInnerOuter {
  buy_amt: number | null
  sell_amt: number | null
  buy_pct: number | null
  sell_pct: number | null
  volume_ratio: number | null
  change_pct: number | null
  position?: string | number | null
}

export interface DarkFlowMnemonic {
  mnemonic: string
  direction?: 'buy' | 'sell' | string | null
  divergence: boolean
  detail?: string | null
}

export interface DarkOrderGroup {
  d: 'B' | 'S' | string
  n: number
  amt: number
  t0: string
  t1: string
  p0: number
  p1: number
  contrarian: boolean
  price_dir: string
  reason: string
}

export interface DarkOrder {
  buy_amt: number
  sell_amt: number
  net: number
  main_buy?: number
  main_sell?: number
  herd_buy: number
  herd_sell: number
  groups: DarkOrderGroup[]
}

export interface L2Summary {
  available: boolean
  cancel_rate?: number | null
  cancel_bias?: number | null
  cancel_signal?: string | null
}

export interface MainflowTri {
  agree: boolean | null
  consensus_wan: number | null
  spread_pct: number | null
  n_ok: number
  sources: {
    tencent?: { net_wan: number | null } | null
    thsdk?: { net_wan: number | null } | null
    tq?: { net_wan: number | null } | null
  }
}

export interface DarkFlowResp {
  main_intent: DarkFlowMainIntent | null
  inner_outer: DarkFlowInnerOuter | null
  mnemonic: DarkFlowMnemonic | null
  dark_order: DarkOrder | null
  /** 2026-09-05 P1: 明盘三源交叉验证 */
  mainflow_tri?: MainflowTri | null
  /** 2026-09-05 P2: L2撤单率(TQ独家) */
  l2?: L2Summary | null
  /** 2026-09-04: 运维可见性(逐笔总数/末笔时刻/页数/停滞标记) */
  diag?: {
    tick_count?: number | null
    last_tick_t?: string | null
    trade_date?: string | null
    tick_pages?: number | null
    stale?: boolean | null
    tick_lag_sec?: number | null
  } | null
}

/** 元 -> 万, 带符号, 无数据返回 '--' */
function fmtWan(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return '--'
  const wan = v / 10000
  const sign = wan > 0 ? '+' : ''
  return `${sign}${wan.toFixed(digits)}万`
}

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '--'
  return `${v.toFixed(digits)}%`
}

function fmtSigned(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '--'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}

function upColor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return 'text-muted-foreground'
  // 涨跌色统一走设计令牌 --stock-up/--stock-down (红涨绿跌, A股口径)
  return v > 0 ? 'text-stock-up' : v < 0 ? 'text-stock-down' : 'text-muted-foreground'
}

export default function DarkFlowCards({ symbol, market }: { symbol: string; market: string }) {
  const [data, setData] = useState<DarkFlowResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>('')
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null)
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    if (!symbol) return
    setError('')
    try {
      const res = await fetchAPI<DarkFlowResp>(`/dark-flow?symbol=${encodeURIComponent(symbol)}`, {
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
    const timer = window.setInterval(() => void load(), 30000) // 与分时图 30s 轮询一致
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [load])

  // 加载中占位
  if (loading && !data) {
    return (
      <div className="mt-3 grid gap-2">
        <div className="h-[92px] rounded-xl border border-border/50 bg-card animate-pulse bg-accent/20" />
        <div className="h-[120px] rounded-xl border border-border/50 bg-card animate-pulse bg-accent/20" />
      </div>
    )
  }

  // 加载失败降级
  if (error && !data) {
    return (
      <div className="mt-3 rounded-xl border border-border/50 bg-card p-3">
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <ShieldAlert className="w-3.5 h-3.5" />
          <span>暗盘/主力意图数据暂不可用({error})</span>
        </div>
      </div>
    )
  }

  const mi = data?.main_intent ?? null
  const io = data?.inner_outer ?? null
  const mn = data?.mnemonic ?? null
  const darkOrder = data?.dark_order ?? null
  const diag = data?.diag ?? null
  const insufficient = mi?.data_status === 'insufficient'

  const askAi = () => {
    const ctx = mn
      ? `主力意图与盘口背离:${mn.mnemonic}${mn.detail ? `(${mn.detail})` : ''} symbol=${symbol}`
      : `主力意图与盘口分析 symbol=${symbol} market=${market}`
    window.dispatchEvent(
      new CustomEvent('panwatch-open-chat', {
        detail: { symbol, market, pageContext: ctx },
      }),
    )
  }

  return (
    <div className="mt-3 grid gap-2">
      {/* ============ 卡片①: 主力意图 ============ */}
      <div className="rounded-xl border border-border/50 bg-card p-3">
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="flex items-center gap-2">
            <div className="text-[13px] font-semibold text-foreground">🎯 主力意图</div>
            {updatedAt && (
              <span className="text-[10px] text-muted-foreground font-mono">
                {updatedAt.toLocaleTimeString('zh-CN', { hour12: false })}
              </span>
            )}
            {/* 2026-09-04 P1-5: 停滞徽标, stale 才出现 */}
            {diag?.stale ? (
              <span
                className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-700 dark:text-amber-400 border border-amber-500/30"
                title={diag?.tick_lag_sec != null ? `末笔落后 ${Math.round(diag.tick_lag_sec / 60)} 分钟` : '逐笔数据停滞'}
              >
                ⚠️ 数据停滞
              </span>
            ) : null}
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

        {insufficient ? (
          <div className="text-[12px] text-amber-700 dark:text-amber-400 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
            数据不足(盘中数据未积累)
          </div>
        ) : mi && mi.main_net != null ? (
          <>
            {mi.signal ? (
              <div className="text-[12px] text-muted-foreground mb-2">{mi.signal}</div>
            ) : null}
            {/* 2026-09-04 P1-4: 结论翻转注记, 有值才展示 */}
            {mi.verdict_note ? (
              <div className="text-[12px] text-sky-700 dark:text-sky-400 bg-sky-500/10 border border-sky-500/20 rounded-lg px-3 py-2 mb-2">
                📢 {mi.verdict_note}
              </div>
            ) : null}
            <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-[11px]">
              <Stat label="主力净额" value={fmtWan(mi.main_net)} valueClass={upColor(mi.main_net)} />
              <Stat label="超大单净额" value={fmtWan(mi.big_net)} valueClass={upColor(mi.big_net)} />
              <Stat label="大单净额" value={fmtWan(mi.mid_net)} valueClass={upColor(mi.mid_net)} />
              <Stat label="散户净额" value={fmtWan(mi.retail_net)} valueClass={upColor(mi.retail_net)} />
              <Stat label="主力参与度" value={fmtPct(mi.main_intensity)} />
              <Stat label="主力买占比" value={fmtPct(mi.main_buy_ratio)} />
            </div>
            {/* 2026-09-04: 数据行(逐笔总数/末笔/页数), 小字不抢戏 */}
            {diag?.tick_count != null ? (
              <div className="mt-2 text-[10px] text-muted-foreground font-mono">
                逐笔 {diag.tick_count} 笔{diag.last_tick_t ? ` · 末笔 ${diag.last_tick_t}` : ''}
                {diag.tick_pages != null ? ` · ${diag.tick_pages} 页` : ''}
              </div>
            ) : null}
            {/* 2026-09-05 P1: 明盘三源交叉(腾讯+thSdk+TQ), 一致绿/分歧 amber */}
            {data?.mainflow_tri && data.mainflow_tri.n_ok >= 2 ? (
              <div
                className={`mt-2 rounded px-2 py-1 text-[10px] leading-4 ${
                  data.mainflow_tri.agree
                    ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                    : 'bg-amber-500/10 text-amber-700 dark:text-amber-400'
                }`}
                title={`腾讯 ${fmtWan((data.mainflow_tri.sources?.tencent?.net_wan ?? NaN) * 10000)} / 同花顺 ${fmtWan((data.mainflow_tri.sources?.thsdk?.net_wan ?? NaN) * 10000)} / 通达信 ${fmtWan((data.mainflow_tri.sources?.tq?.net_wan ?? NaN) * 10000)}`}
              >
                {data.mainflow_tri.agree
                  ? `✓ 三源一致 ${fmtWan((data.mainflow_tri.consensus_wan ?? 0) * 10000)}`
                  : `⚠️ 三源分歧(离散 ${data.mainflow_tri.spread_pct ?? '--'}%)，方向仅供参考`}
              </div>
            ) : null}
            {/* 2026-09-05 P2: 撤单率(TQ独家), 有信号才展示 */}
            {data?.l2?.cancel_signal ? (
              <div
                className="mt-2 rounded bg-violet-500/10 px-2 py-1 text-[10px] leading-4 text-violet-700 dark:text-violet-400"
                title={`撤单率 ${data.l2.cancel_rate ?? '--'}% · 撤单偏向 ${data.l2.cancel_bias ?? '--'}%(+买方撤多/-卖方撤多)`}
              >
                🔄 {data.l2.cancel_signal}({data.l2.cancel_rate ?? '--'}%)
              </div>
            ) : null}
          </>
        ) : (
          <div className="text-[12px] text-muted-foreground">暂无主力意图数据(非A股/未开盘/数据源不可用)</div>
        )}
      </div>

      {/* ============ 卡片: 暗盘资金(拆单识别, 2026-08-30) ============ */}
      {darkOrder && darkOrder.net != null ? (
        <div className="rounded-xl border border-border/50 bg-card p-3">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[13px] font-semibold text-foreground">🕵️ 暗盘资金(拆单识别)</div>
            <span className="text-[10px] text-muted-foreground">主力伪装的中小单·逆势+位置确认</span>
          </div>
          {/* 2026-09-04 C计划定论: 涨停日散户获利了结潮与主力拆单在L1逐笔不可区分,
              涨停股暗盘方向可能反, 降权提示(非涨停股对账1.01/0.76可信)。 */}
          {data?.inner_outer?.change_pct != null && data.inner_outer.change_pct >= 9.5 && (
            <div className="mb-2 rounded bg-amber-500/10 px-2 py-1 text-[10px] leading-4 text-amber-700 dark:text-amber-400">
              涨停日散户潮污染：获利了结盘与主力拆单无法区分，方向仅供参考
            </div>
          )}
          <div className="grid grid-cols-3 gap-2 text-[11px]">
            <Stat label="暗盘净额" value={fmtWan(darkOrder.net)} valueClass={upColor(darkOrder.net)} />
            <Stat label="疑似主力买" value={fmtWan(darkOrder.main_buy ?? darkOrder.buy_amt)} valueClass={upColor(darkOrder.main_buy ?? darkOrder.buy_amt)} />
            <Stat label="疑似主力卖" value={fmtWan(darkOrder.main_sell ?? darkOrder.sell_amt)} valueClass={upColor(darkOrder.main_sell ?? darkOrder.sell_amt)} />
            <Stat label="散户买(顺势)" value={fmtWan(darkOrder.herd_buy)} valueClass={upColor(darkOrder.herd_buy)} />
            <Stat label="散户卖(解套)" value={fmtWan(darkOrder.herd_sell)} valueClass={upColor(darkOrder.herd_sell)} />
          </div>
          {darkOrder.groups?.length ? (
            <div className="mt-2 space-y-1 border-t border-border/50 pt-2">
              {darkOrder.groups.slice(0, 5).map((g, i) => (
                <div key={i} className="flex items-center justify-between text-[11px]">
                  <span className={`font-mono ${g.d === 'B' ? 'text-stock-up' : 'text-stock-down'}`}>
                    {g.d === 'B' ? '买' : '卖'} {g.n}笔 {fmtWan(g.amt)}
                  </span>
                  <span className={g.contrarian ? 'text-amber-700 dark:text-amber-400 font-medium' : 'text-muted-foreground'}>
                    {g.reason}{g.contrarian ? '·主力' : ''} @ {g.t0}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {/* ============ 卡片②: 内盘外盘 ============ */}
      <div className="rounded-xl border border-border/50 bg-card p-3">
        <div className="text-[13px] font-semibold text-foreground mb-2">📊 内盘外盘</div>

        {io && (io.buy_pct != null || io.sell_pct != null) ? (
          <>
            {/* 内外盘占比进度条: 外盘(买)红 / 内盘(卖)绿 */}
            <div className="flex items-center gap-2 text-[11px] mb-1">
              <span className="text-stock-up font-mono">外盘 {fmtPct(io.buy_pct)}</span>
              <div className="flex-1 h-1.5 rounded-full bg-accent/40 overflow-hidden flex">
                <div
                  className="h-full bg-stock-up/80"
                  style={{ width: `${Math.min(100, Math.max(0, io.buy_pct ?? 0))}%` }}
                />
                <div
                  className="h-full bg-stock-down/80"
                  style={{ width: `${Math.min(100, Math.max(0, io.sell_pct ?? 0))}%` }}
                />
              </div>
              <span className="text-stock-down font-mono">内盘 {fmtPct(io.sell_pct)}</span>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-[11px] mt-2">
              <Stat label="量比" value={io.volume_ratio != null ? io.volume_ratio.toFixed(2) : '--'} />
              <Stat
                label="涨跌"
                value={fmtSigned(io.change_pct)}
                valueClass={upColor(io.change_pct)}
              />
              <Stat
                label="位置"
                value={typeof io.position === 'string' ? io.position : io.position != null ? String(io.position) : '--'}
              />
              <Stat label="外盘额" value={fmtWan(io.buy_amt)} valueClass={upColor(io.buy_amt)} />
              <Stat
                label="主动盘占比"
                value={
                  io.buy_pct != null && io.sell_pct != null
                    ? `${(io.buy_pct + io.sell_pct).toFixed(1)}%`
                    : '--'
                }
              />
            </div>
          </>
        ) : (
          <div className="text-[12px] text-muted-foreground">暂无内外盘数据(非A股/未开盘/数据源不可用)</div>
        )}

        {/* 口诀提示 */}
        {mn ? (
          <div className="mt-2 rounded-lg bg-accent/20 border border-border/50 px-3 py-2 flex items-start justify-between gap-2">
            <div className="text-[12px] leading-relaxed">
              <span className="font-medium text-foreground">⚠️ {mn.mnemonic}</span>
              {mn.detail ? <span className="text-muted-foreground ml-1">— {mn.detail}</span> : null}
            </div>
            {mn.divergence ? (
              <Button variant="secondary" size="sm" className="h-7 px-2.5 shrink-0" onClick={askAi}>
                <Bot className="w-3.5 h-3.5 mr-1" /> 咨询AI助手
              </Button>
            ) : null}
          </div>
        ) : (
          <div className="mt-2 text-[12px] text-muted-foreground">内外盘均衡/无明显口诀信号</div>
        )}
      </div>

      {/* ============ 卡片: 决策先锋三指标(2026-08-30) ============ */}
      <DecisionPioneerCard symbol={symbol} market={market} />

      {/* ============ 卡片③: OB 盘口失衡条(2026-08-20) — 底部挂载 ============ */}
      <OrderBookObBar symbol={symbol} />

      {/* ============ 卡片④: 主力意图双源对比(2026-08-20, v0.3.0) ============ */}
      <MainFlowCompareCard symbol={symbol} />
    </div>
  )
}

function Stat({
  label,
  value,
  valueClass,
}: {
  label: string
  value: string
  valueClass?: string
}) {
  return (
    <div className="rounded-lg bg-accent/20 px-2.5 py-1.5">
      <div className="text-muted-foreground">{label}</div>
      <div className={`font-mono ${valueClass ?? 'text-foreground'}`}>{value}</div>
    </div>
  )
}
