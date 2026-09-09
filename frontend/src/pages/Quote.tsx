import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { useSearchParams, useParams, Link } from 'react-router-dom'
import { Loader2, Search } from 'lucide-react'

import KlineChart from '@panwatch/biz-ui/components/KlineChart'
import SectionHeader from '@panwatch/biz-ui/components/SectionHeader'
import { insightApi, dashboardApi, datasourcesApi, type DashboardPosition, type VendorTrustItem } from '@panwatch/api'
import { useSourceHealth } from '@/hooks/useSourceHealth'
import {
  normalizeKlineEvents,
  normalizePriceLines,
  type KlineEventPoint,
  type KlinePriceLine,
} from '@panwatch/biz-ui/klineEvents'
import { toAmount, toWan } from '@/lib/format'

/**
 * 行情终端（v0.4.60 重构 — 去卡片化）
 *
 * 核心原则(设计稿 §1):
 * - K线大图是绝对主角(≥80% 屏宽, 无卡片包)
 * - 信息密度按"决策需要"分层, 不是堆卡片
 * - "决策三问" 在顶部一行紧凑文字
 * - 主力意图 / 暗盘簇 / 事件按窄栏紧凑列表呈现
 * - 数据明细(fund_flow 120 行 / events 472 条) 默认折叠
 *
 * URL 参数: ?symbol=002361&type=stock&source=holdings(可选 context 卡)
 */

const ForecastPage = lazy(() => import('@/pages/Forecast'))

type QuoteType = 'stock' | 'index' | 'board'

/** 上次查看的股票(09-03: 打开行情页默认回到上次, 仅首次无记录时 fallback 上证) */
const LAST_SYMBOL_KEY = 'quote:lastSymbol'
function readLastSymbol(): string | null {
  try {
    const v = localStorage.getItem(LAST_SYMBOL_KEY)?.trim()
    return v || null
  } catch {
    return null
  }
}
function writeLastSymbol(s: string) {
  try {
    if (s.trim()) localStorage.setItem(LAST_SYMBOL_KEY, s.trim())
  } catch {
    /* 无痕模式等写失败不抛 */
  }
}

interface StockSuggest {
  symbol: string
  name: string
  market?: string
}

const TYPE_OPTIONS: Array<{ key: QuoteType; label: string }> = [
  { key: 'stock', label: '个股' },
  { key: 'index', label: '指数' },
  { key: 'board', label: '板块' },
]

const QUICK_INDICES = [
  { symbol: '000001', name: '上证指数' },
  { symbol: '399001', name: '深证成指' },
  { symbol: '399006', name: '创业板指' },
]

interface FundFlowRow {
  date: string
  ming_net: number | null
  dark_net: number | null
}

interface DarkClustersInfo {
  available: boolean
  note?: string
  dark_net?: number | null
  dark_buy?: number | null
  dark_sell?: number | null
  cluster_count?: number
  ming_net?: number | null
  main_net?: number | null
  cancel_rate?: number | null
  active_passive_ratio?: number | null
}

interface OrderbookInfo {
  available?: boolean
  shape?: string | null
  best_bid?: number | null
  best_ask?: number | null
  spread?: number | null
  bid_pressure?: number | null
}

interface ChipsInfo {
  peak_price?: number | null
  cost_10?: number | null
  cost_50?: number | null
  cost_90?: number | null
}

/** 决策先锋三指标共振状态(后端 summary.resonance, 全部可选以容错) */
interface ResonanceInfo {
  available?: boolean
  row?: number | null
  phase?: string | null
  action_label?: string | null
  action_text?: string | null
  tone?: string | null
  bad_count?: number | null
}

interface SummaryResp {
  symbol?: string
  market?: string
  fund_flow?: FundFlowRow[]
  activity_series?: Array<{
    date?: string | null
    activity?: number | null
    level?: string | null
  }> | null
  events?: Array<{
    date?: string | null
    kind?: string | null
    label?: string | null
    price?: number | null
  }>
  orderbook?: OrderbookInfo | null
  main_intent?: string | null
  dark_clusters?: DarkClustersInfo | null
  unlock_levels?: KlinePriceLine[] | null
  chips?: ChipsInfo | null
  gs_signals?: Array<{
    date?: string | null
    side?: string | null
    confirmed?: boolean
  }> | null
  resonance?: ResonanceInfo | null
  /** A4 置信度徽章(批次A): mainflow_tri + confidence 载荷 */
  mainflow_tri?: {
    confidence_level?: string | null
    sources_agree?: boolean | null
    spread_pct?: number | null
    n_ok?: number | null
    consensus_wan?: number | null
  } | null
}

/** A3 封单成色(批次A): /api/seal-quality 响应 */
interface SealQualityMetrics {
  available?: boolean
  reason?: string | null
  window_min?: number | null
  cancel_rate_5m?: number | null
  seal_quality?: number | null
  cancel_bias_5m?: number | null
  cancel_zscore?: number | null
  seal_success_rate?: number | null
  is_sealed?: boolean | null
  seal_amount?: number | null
}
interface SealQualityResp {
  symbol?: string
  n_samples?: number
  metrics?: SealQualityMetrics | null
}

/** KI-021: GET /api/decision/{symbol} 决策合成(后端永不 500, 缺数→看看) */
interface DecisionResp {
  symbol?: string
  verdict?: string
  reason?: string
  phase?: string
  row?: number
  parts?: {
    trend?: string | null
    activity?: number | null
    fund_net?: number | null
  } | null
}

/** 决策合成 verdict 配色: 动手=红(A股买色)/别碰=绿(卖色)/看看=琥珀 */
const DECISION_CLASS: Record<string, string> = {
  动手: 'text-stock-up',
  别碰: 'text-stock-down',
  看看: 'text-amber-600 dark:text-amber-400',
}

/** 置信度徽章配色: A=可信(主色)/B=单源(灰)/C=分歧(琥珀, 非涨跌语义) */
const CONFIDENCE_CLASS: Record<string, string> = {
  A: 'bg-primary/10 text-foreground',
  B: 'bg-muted text-muted-foreground',
  C: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
}

/* toAmount/toWan 统一走 @/lib/format(P3 收敛, 旧手抄版删除) */

/** 红涨绿跌(国内 A 股惯例, 按用户 override 设计稿 §5.2) — 统一走 --stock-up/--stock-down 令牌 */
const NET_INFLOW_CLASS = (v: number | null | undefined) =>
  v == null ? 'text-muted-foreground' : v > 0 ? 'text-stock-up' : v < 0 ? 'text-stock-down' : 'text-foreground'

/** GS 信号(已确认/待确认) — 买红卖绿, 收敛 P0 stock 令牌(原 rose/emerald 硬编码) */
const GS_COLOR = (side: 'G' | 'S', confirmed: boolean) => {
  const filled = confirmed ? '' : 'opacity-60 ring-1 ring-current'
  return side === 'G' ? `text-stock-up ${filled}` : `text-stock-down ${filled}`
}

/** 决策共振 tone 上色 — 与红涨绿跌一致: bull→红(涨色) / bear→绿(跌色) / warn→琥珀 / neutral→灰 */
const RESONANCE_TONE_CLASS = (tone: string | null | undefined) =>
  tone === 'bull'
    ? 'text-stock-up'
    : tone === 'bear'
      ? 'text-stock-down'
      : tone === 'warn'
        ? 'text-amber-500'
        : 'text-muted-foreground'

export default function QuotePage() {
  const [params, setParams] = useSearchParams()
  const routeSymbol = useParams().symbol
  // W3.7/D7: 支持 path 式 /quote/600519(URL 直达/收藏) —— 规范化为 query 式后复用
  // 单一 symbol 来源; 用户站内切换仍走 query, 不会出现 path/query 双源打架。
  useEffect(() => {
    if (routeSymbol && !params.get('symbol')) {
      setParams({ symbol: routeSymbol.trim() }, { replace: true })
    }
  }, [routeSymbol, params, setParams])
  // 默认股票: URL > 上次查看(localStorage) > 上证指数(仅首次)
  const symbol = (params.get('symbol') || readLastSymbol() || '000001').trim()
  const type = (params.get('type') as QuoteType) || 'stock'

  const [input, setInput] = useState(symbol)
  const [suggests, setSuggests] = useState<StockSuggest[]>([])
  const [suggestOpen, setSuggestOpen] = useState(false)

  // 09-03: symbol 落定即记住为"上次查看"
  useEffect(() => {
    writeLastSymbol(symbol)
  }, [symbol])

  // 09-03: 名称/代码联想(300ms debounce, 后端 /api/stocks/search)
  useEffect(() => {
    const q = input.trim()
    if (q.length < 1 || q === symbol) {
      setSuggests([])
      return
    }
    const t = window.setTimeout(() => {
      insightApi
        .searchStocks<StockSuggest[]>(q)
        .then((d) => {
          setSuggests(Array.isArray(d) ? d.slice(0, 8) : [])
          setSuggestOpen(true)
        })
        .catch(() => setSuggests([]))
    }, 300)
    return () => window.clearTimeout(t)
  }, [input, symbol])
  const [summary, setSummary] = useState<SummaryResp | null>(null)
  const [, setSummaryLoading] = useState(false)
  const [, setSummaryError] = useState('')
  // KI-019 来源徽标: 当前行情由哪个 vendor 提供 + 采集延迟(空源显式标"未知")
  const [quoteSrc, setQuoteSrc] = useState<{ source: string; latency: number } | null>(null)
  const [vendorTrust, setVendorTrust] = useState<Record<string, VendorTrustItem>>({})
  // KI-021 决策合成卡片(后端三信号 → 动手/看看/别碰)
  const [decisionCard, setDecisionCard] = useState<DecisionResp | null>(null)

  // A3 封单成色(批次A, 2026-09-06): 只在指标可用时渲染, 无数据不占位(缺失不编造)
  const [seal, setSeal] = useState<SealQualityResp | null>(null)
  useEffect(() => {
    if (!symbol || type !== 'stock') return
    let alive = true
    insightApi
      .sealQuality<SealQualityResp>(symbol)
      .then((d) => { if (alive) setSeal(d ?? null) })
      .catch(() => { if (alive) setSeal(null) })
    return () => { alive = false }
  }, [symbol, type])

  // v0.4.60: 联动状态全页共享, 不止 Fund Tab
  const [selectedRange, setSelectedRange] = useState<{ from: string; to: string } | null>(null)
  const [hoveredDate, setHoveredDate] = useState<string | null>(null)

  // 4 图层开关
  const [layers, setLayers] = useState({
    trend: true,
    signal: true,
    capital: true,
    event: true,
  })

  // 数据明细折叠
  const [detailOpen, setDetailOpen] = useState(false)

  // 副图切换(成交量/MACD)
  const [subchart, setSubchart] = useState<'vol' | 'macd' | 'activity'>('vol')

  const sourceHealth = useSourceHealth()
  useEffect(() => { setInput(symbol) }, [symbol])

  useEffect(() => {
    if (!symbol || type === 'board') return
    let alive = true
    setSummaryLoading(true)
    setSummaryError('')
    insightApi
      .klineSummary<SummaryResp>(symbol, 'CN')
      .then((d) => { if (alive) setSummary(d ?? null) })
      .catch((e: unknown) => {
        if (alive) setSummaryError(e instanceof Error ? e.message : '加载失败')
      })
      .finally(() => { if (alive) setSummaryLoading(false) })
    return () => { alive = false }
  }, [symbol, type])

  // KI-019: 行情来源徽标 —— 取当前 quote 的 source/source_latency_ms(空源=未知, 不编造)
  useEffect(() => {
    if (!symbol || type !== 'stock') { setQuoteSrc(null); return }
    let alive = true
    insightApi
      .quote<{ source?: string | null; source_latency_ms?: number | null }>(symbol, 'CN')
      .then((q) => {
        if (!alive) return
        setQuoteSrc({ source: String(q?.source || ''), latency: Number(q?.source_latency_ms || 0) })
      })
      .catch(() => { if (alive) setQuoteSrc(null) })
    return () => { alive = false }
  }, [symbol, type])

  // KI-021: 决策合成卡片(独立请求, 不阻塞 K线; 失败静默不渲染)
  useEffect(() => {
    if (!symbol || type !== 'stock') { setDecisionCard(null); return }
    let alive = true
    insightApi
      .decision<DecisionResp>(symbol, 'CN')
      .then((d) => { if (alive) setDecisionCard(d ?? null) })
      .catch(() => { if (alive) setDecisionCard(null) })
    return () => { alive = false }
  }, [symbol, type])

  // KI-019: vendor 质量分(60s 轮询足够; 失败静默 —— 徽标退化为只显 source+延迟)
  useEffect(() => {
    let alive = true
    const load = () => {
      datasourcesApi
        .trust()
        .then((d) => {
          if (!alive) return
          const map: Record<string, VendorTrustItem> = {}
          for (const it of d?.items ?? []) if (it?.vendor) map[it.vendor] = it
          setVendorTrust(map)
        })
        .catch(() => { /* 静默: 无质量分不阻断行情 */ })
    }
    load()
    const timer = window.setInterval(load, 60_000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [])

  // Phase 0: 持仓成本(全账户 positions 按代码匹配, 无持仓不画线不编造)
  const [positions, setPositions] = useState<DashboardPosition[]>([])
  useEffect(() => {
    let alive = true
    dashboardApi
      .portfolioSummary()
      .then((d) => {
        if (!alive) return
        const all = (d?.accounts ?? []).flatMap((a) => a.positions ?? [])
        setPositions(all)
      })
      .catch(() => { if (alive) setPositions([]) })
    return () => { alive = false }
  }, [])
  /** 代码归一(去后缀/取后6位数字): 002361.SZ 与 002361 视为同一标的 */
  const normCode = (s: string) => (s.replace(/\D/g, '').slice(-6) || s.trim())
  const costLines = useMemo(() => {
    if (type !== 'stock') return undefined
    const hit = positions.find((p) => normCode(p.symbol) === normCode(symbol))
    if (!hit || !Number.isFinite(hit.cost_price)) return undefined
    const qty = hit.quantity > 0 ? `·${hit.quantity}股` : ''
    return [{ price: hit.cost_price, title: `成本${hit.cost_price}${qty}` }]
  }, [positions, symbol, type])

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next)
  }

  const submitSymbol = () => {
    const v = input.trim()
    if (!v) return
    setSuggestOpen(false)
    // 名称直输回车: 优先精确命中候选, 否则非代码串取首个联想; 代码直通过
    const hit =
      suggests.find((s) => s.symbol === v || s.name === v) ??
      (/^\d{6}$/.test(v) ? null : suggests[0])
    setParam('symbol', hit ? hit.symbol : v)
  }

  const layerToggle = (key: keyof typeof layers) =>
    setLayers((s) => ({ ...s, [key]: !s[key] }))

  // 标准化数据
  const normEvents = useMemo(
    () => normalizeKlineEvents(summary?.events ?? null),
    [summary?.events],
  )
  const normPriceLines = useMemo(
    () => normalizePriceLines(summary?.unlock_levels ?? null),
    [summary?.unlock_levels],
  )
  const normGsSignals = useMemo(
    () =>
      (summary?.gs_signals ?? [])
        .filter((g) => g && g.date && (g.side === 'G' || g.side === 'S'))
        .map((g) => ({
          date: g.date as string,
          side: g.side as 'G' | 'S',
          confirmed: !!g.confirmed,
        })),
    [summary?.gs_signals],
  )

  // 决策三问的快速判断(基于现有数据, 不编造)
  const decision = useMemo(() => {
    const dc = summary?.dark_clusters
    const ob = summary?.orderbook
    const lastFund = summary?.fund_flow?.slice(-1)?.[0]
    const lastGs = summary?.gs_signals?.slice(-1)?.[0]
    // 该不该动: GS末条 + 暗盘净额
    const action =
      lastGs && !lastGs.confirmed
        ? '观望（盘中信号未收盘确认）'
        : lastGs?.side === 'G'
          ? '可关注（GS 买）'
          : lastGs?.side === 'S'
            ? '谨慎（GS 卖）'
            : '无明确信号'
    // 主力在干嘛: 暗盘 main_net(P2-11: null/NaN 显式无数据, 不按 0 算中性)
    const mainRaw = dc?.available ? dc.main_net : lastFund?.ming_net
    const mainNum = mainRaw == null ? NaN : Number(mainRaw)
    const mainDesc = !Number.isFinite(mainNum) ? '无数据' : mainNum > 0 ? `${toAmount(mainNum).replace(/^\+?/, '+')}` : mainNum < 0 ? `${toAmount(mainNum).replace(/^\+?/, '')}` : '数据中性'
    // 风险在哪: 撤单率 + 涨跌停事件
    const cancelRate = dc?.cancel_rate
    const limits = (summary?.events ?? []).filter((e) => e.kind === 'limit_up' || e.kind === 'limit_down').length
    const riskDesc =
      limits > 0 ? `近期有 ${limits} 次涨跌停（高波动）` :
      cancelRate != null && cancelRate > 0.3 ? `撤单率 ${(cancelRate * 100).toFixed(1)}%（主力试探）` :
      cancelRate != null ? `撤单率 ${(cancelRate * 100).toFixed(1)}%` :
      '正常波动'
    return { action, mainDesc, riskDesc, lastSide: lastGs?.side, lastConfirmed: lastGs?.confirmed, obShape: ob?.shape }
  }, [summary])

  // KI-019: 来源徽标文案(未知源显式标, 不编造; 质量分来自 /datasources/trust)
  const srcBadge = useMemo(() => {
    if (!quoteSrc) return null
    const vendor = quoteSrc.source.trim()
    const t = vendor ? vendorTrust[vendor] : undefined
    const tips = [vendor ? `行情来源: ${vendor}` : '行情来源: 未知(后端未透传 source)']
    if (quoteSrc.latency > 0) tips.push(`采集延迟 ${quoteSrc.latency}ms`)
    if (t) {
      tips.push(`质量分 ${t.score}`)
      if (t.success_rate != null) tips.push(`成功率 ${(t.success_rate * 100).toFixed(0)}%`)
      if (t.p50_latency_ms != null) tips.push(`P50 ${t.p50_latency_ms}ms`)
    } else if (vendor) {
      tips.push('暂无质量分样本')
    }
    return {
      label: vendor || '未知',
      latency: quoteSrc.latency,
      title: tips.join('；'),
    }
  }, [quoteSrc, vendorTrust])

  // 区间聚合(联动 K线选段)
  const rangeAgg = useMemo(() => {
    if (!selectedRange || !summary?.fund_flow) return null
    const rows = summary.fund_flow.filter(
      (r) => r.date >= selectedRange.from && r.date <= selectedRange.to,
    )
    if (rows.length === 0) return null
    return {
      count: rows.length,
      ming: rows.reduce((s, r) => s + (r.ming_net ?? 0), 0),
      dark: rows.reduce((s, r) => s + (r.dark_net ?? 0), 0),
    }
  }, [selectedRange, summary?.fund_flow])

  return (
    <div className="sida-page-enter w-full space-y-3">
      {/* === 顶部数据条(不是卡片, 是 flex 数据条) === */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border/40 pb-2">
        <h1 className="text-base font-semibold text-foreground">行情</h1>
        <div className="inline-flex items-center gap-1 rounded-md border border-border/50 p-0.5 text-[11px]">
          {TYPE_OPTIONS.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => setParam('type', t.key)}
              className={`rounded-md px-2 py-0.5 transition-colors ${
                type === t.key
                  ? 'bg-primary text-primary-foreground'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="flex flex-1 items-center gap-1 min-w-[200px]">
          <div className="relative min-w-0 flex-1">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submitSymbol()
                if (e.key === 'Escape') setSuggestOpen(false)
              }}
              onFocus={() => {
                if (suggests.length > 0) setSuggestOpen(true)
              }}
              onBlur={() => {
                // 延迟关闭, 让候选项 onMouseDown 先触发
                window.setTimeout(() => setSuggestOpen(false), 120)
              }}
              placeholder="代码/名称, 如 002361 或 神剑"
              className="h-7 w-full min-w-0 rounded-md border border-border/50 bg-background px-2 text-[12px] outline-none focus:border-primary/50"
            />
            {suggestOpen && suggests.length > 0 && (
              <div className="absolute left-0 right-0 top-8 z-30 overflow-hidden rounded-md border border-border/60 bg-popover shadow-lg">
                {suggests.map((s) => (
                  <button
                    key={`${s.market ?? 'CN'}:${s.symbol}`}
                    type="button"
                    onMouseDown={(e) => {
                      e.preventDefault()
                      setInput(s.symbol)
                      setSuggestOpen(false)
                      setParam('symbol', s.symbol)
                    }}
                    className="flex w-full items-center justify-between gap-2 px-2 py-1.5 text-left text-[12px] hover:bg-accent"
                  >
                    <span className="font-medium text-foreground">{s.name}</span>
                    <span className="font-mono text-muted-foreground">{s.symbol}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={submitSymbol}
            className="inline-flex h-7 shrink-0 items-center gap-1 rounded-md border border-border/50 bg-background px-2 text-[11px] text-muted-foreground hover:text-foreground"
          >
            <Search className="h-3 w-3" />
            查询
          </button>
        </div>
        {type === 'index' && (
          <div className="flex items-center gap-1 text-[11px]">
            <span className="text-muted-foreground">指数:</span>
            {QUICK_INDICES.map((i) => (
              <button
                key={i.symbol}
                type="button"
                onClick={() => setParam('symbol', i.symbol)}
                className={`rounded-md border px-1.5 py-0.5 transition-colors ${
                  symbol === i.symbol
                    ? 'border-primary/40 bg-primary/10 text-foreground'
                    : 'border-border/50 text-muted-foreground hover:text-foreground'
                }`}
              >
                {i.name}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* === 决策三问(顶部条带, 不是卡片) === */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-border/40 pb-2 text-[12px]">
        <span className="text-muted-foreground" title="综合 GS 策略信号 + 暗盘主力净额给出的操作倾向(仅供参考, 不构成投资建议)">该不该动:</span>
        <span className="font-medium text-foreground">{decision.action}</span>
        <span className="text-border/60">|</span>
        <span className="text-muted-foreground" title="主力资金方向: 明盘三源(thsdk/腾讯/东财)交叉 + 暗盘 TQ 口径">主力:</span>
        <span className="font-mono">{decision.mainDesc}</span>
        <span className="text-border/60">|</span>
        <span className="text-muted-foreground" title="风险提示: 按近期涨跌停/波动率等规则生成">风险:</span>
        <span className="font-medium text-foreground">{decision.riskDesc}</span>
        {summary?.orderbook?.shape && (
          <>
            <span className="text-border/60">|</span>
            <span className="text-muted-foreground" title="盘口形态: 按十档买卖额结构判定(均衡/买盘占优/卖盘占优)">盘口:</span>
            <span className="font-medium text-foreground">{summary.orderbook.shape}</span>
            <Link to={`/l2?symbol=${encodeURIComponent(symbol)}`} className="text-[11px] text-primary hover:underline">明细›</Link>
          </>
        )}
        {srcBadge && (
          <>
            <span className="text-border/60">|</span>
            <span className="text-muted-foreground" title="当前行情由哪个数据源提供及其采集延迟(质量分来自 /api/datasources/trust)">源:</span>
            <span className="font-mono text-muted-foreground" title={srcBadge.title}>
              {srcBadge.label}{srcBadge.latency > 0 ? ` · ${srcBadge.latency}ms` : ''}
            </span>
          </>
        )}
      </div>

      {/* === KI-021 决策合成卡片(后端三信号 → 动手/看看/别碰; 缺数诚实标 —) === */}
      {decisionCard && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border/40 bg-accent/10 px-3 py-1.5 text-[12px]">
          <span
            className="text-muted-foreground"
            title="后端合成: 趋势(GS) × 活跃度(AI) × 资金(明+暗主力净额) → 7 行状态表; 与上方「该不该动」的前端快判口径不同, 以后者仅作参考"
          >
            决策合成:
          </span>
          <span className={`font-semibold ${DECISION_CLASS[decisionCard.verdict || ''] || 'text-foreground'}`}>
            {decisionCard.verdict || '看看'}
          </span>
          <span className="text-foreground/90">{decisionCard.reason || '—'}</span>
          <span className="font-mono text-[11px] text-muted-foreground" title="三信号明细: 趋势/活跃度/资金(明+暗主力净额, 万元)">
            趋势 {decisionCard.parts?.trend || '—'}
            {' · '}活跃度 {decisionCard.parts?.activity != null ? Number(decisionCard.parts.activity).toFixed(2) : '—'}
            {' · '}资金 {decisionCard.parts?.fund_net != null ? `${(Number(decisionCard.parts.fund_net) / 1e4).toFixed(0)}万` : '—'}
          </span>
        </div>
      )}

      {/* === 两栏布局: K线主图(≥80%屏宽) + 窄栏(决策依据/事件) === */}
      {type === 'board' ? (
        <div className="border-l-2 border-border/40 pl-4 py-8 text-center text-[13px] text-muted-foreground">
          板块行情请前往{' '}
          <Link to={`/boards/${encodeURIComponent(symbol)}`} className="text-primary hover:underline">
            板块详情 /boards/{symbol} ›
          </Link>
        </div>
      ) : (
        <div className="grid grid-cols-12 gap-3">
          {/* === K线主图栏 === */}
          <div className="col-span-12 lg:col-span-9 space-y-2">
            {/* 图层开关条 */}
            <div className="flex flex-wrap items-center gap-2 text-[11px]">
              <span className="text-muted-foreground">图层:</span>
              {(
                [
                  ['trend', 'L1 趋势'],
                  ['signal', 'L2 买卖点'],
                  ['capital', 'L3 资金柱'],
                  ['event', 'L4 事件'],
                ] as const
              ).map(([k, label]) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => layerToggle(k)}
                  className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 transition-colors ${
                    layers[k]
                      ? 'border-primary/40 bg-primary/10 text-foreground'
                      : 'border-border/40 bg-background text-muted-foreground line-through opacity-60'
                  }`}
                >
                  <span className={`h-1 w-1 rounded-full ${layers[k] ? 'bg-primary' : 'bg-muted'}`} />
                  {label}
                </button>
              ))}
              <span className="text-border/60 mx-1">副图:</span>
              {(['vol', 'macd', 'activity'] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setSubchart(s)}
                  className={`rounded-md border px-1.5 py-0.5 ${
                    subchart === s
                      ? 'border-primary/40 bg-primary/10 text-foreground'
                      : 'border-border/40 text-muted-foreground hover:text-foreground'
                  }`}
                >
                  {s === 'vol' ? '成交量' : s === 'macd' ? 'MACD' : '活跃度'}
                </button>
              ))}
            </div>
            {/* K线大图(无卡片包) */}
            <KlineChart
              key={`${type}:${symbol}`}
              symbol={symbol}
              market="CN"
              initialInterval="1d"
              initialDays={120}
              events={normEvents}
              supportPressure={normPriceLines}
              costLines={costLines}
              gsSignals={normGsSignals}
              fundFlow={summary?.fund_flow?.map((r) => ({
                date: r.date,
                open_net: r.ming_net,
                dark_net: r.dark_net,
              }))}
              activitySeries={(summary?.activity_series ?? [])
                .filter((r) => r.date != null)
                .map((r) => ({
                  date: r.date as string,
                  activity: r.activity ?? null,
                  level: r.level ?? null,
                }))}
              layersVisible={layers}
              subchart={subchart}
              onRangeSelect={setSelectedRange}
              onCrosshairMove={(p) => setHoveredDate(p?.time ? p.time.slice(0, 10) : null)}
            />
          </div>

          {/* === 窄栏: 主力意图/暗盘簇/事件(紧凑列表, 无卡片) === */}
          <div className="col-span-12 lg:col-span-3 space-y-3 border-l border-border/40 pl-3 text-[12px]">
            {/* 区间联动摘要 */}
            {rangeAgg && (
              <div className="border-b border-border/40 pb-2 text-[11px] text-muted-foreground">
                <div>区间 {selectedRange!.from} ~ {selectedRange!.to}</div>
                <div className="mt-0.5">
                  明盘 <span className={`font-mono ${NET_INFLOW_CLASS(rangeAgg.ming)}`}>{toWan(rangeAgg.ming)}</span>
                  {' · '}暗盘 <span className={`font-mono ${NET_INFLOW_CLASS(rangeAgg.dark)}`}>{toWan(rangeAgg.dark)}</span>
                </div>
                <div className="text-[10px] opacity-70">{rangeAgg.count} 个交易日</div>
              </div>
            )}

            {/* 主力意图(短描述) + A4 置信度徽章 */}
            {typeof summary?.main_intent === 'string' && summary.main_intent ? (
              <div className="border-b border-border/40 pb-2">
                <div className="flex items-center justify-between">
                  <div className="text-[11px] text-muted-foreground">主力意图</div>
                  {summary.mainflow_tri?.confidence_level && (
                    <span
                      className={`rounded px-1 text-[10px] ${CONFIDENCE_CLASS[summary.mainflow_tri.confidence_level] ?? ''}`}
                      title={`明盘三源交叉: ${summary.mainflow_tri.n_ok ?? 0} 源有数, 离散 ${summary.mainflow_tri.spread_pct ?? '--'}%`}
                    >
                      置信 {summary.mainflow_tri.confidence_level}
                    </span>
                  )}
                </div>
                <div className="mt-0.5 text-foreground whitespace-pre-wrap leading-snug">
                  {summary.main_intent.split('\n').slice(0, 3).join(' · ')}
                </div>
              </div>
            ) : null}

            {/* A3 封单成色(批次A): 涨停股盘中采样, 撤单率异动比炸板早几分钟 */}
            {seal?.metrics?.available && (
              <div className="border-b border-border/40 pb-2">
                <div className="text-[11px] text-muted-foreground">
                  封单成色
                  <span className="ml-1 text-[10px] opacity-70">
                    近{Math.round(seal.metrics.window_min ?? 0)}分钟 · 盘中60s采样
                  </span>
                </div>
                <div className="mt-1 grid grid-cols-2 gap-x-2 gap-y-0.5">
                  <div className="text-muted-foreground">成色</div>
                  <div
                    className={`text-right font-mono ${
                      (seal.metrics.seal_quality ?? 1) < 0.6 || (seal.metrics.cancel_zscore ?? 0) >= 2
                        ? 'text-amber-600 dark:text-amber-400'
                        : 'text-foreground'
                    }`}
                  >
                    {seal.metrics.seal_quality != null
                      ? `${(seal.metrics.seal_quality * 100).toFixed(0)}%`
                      : '-'}
                  </div>
                  <div className="text-muted-foreground">撤单率</div>
                  <div className="text-right font-mono">
                    {seal.metrics.cancel_rate_5m != null
                      ? `${(seal.metrics.cancel_rate_5m * 100).toFixed(1)}%`
                      : '-'}
                  </div>
                  <div className="text-muted-foreground">撤单异动</div>
                  <div className="text-right font-mono">
                    {seal.metrics.cancel_zscore != null ? seal.metrics.cancel_zscore : '-'}
                  </div>
                  <div className="text-muted-foreground">封板成功率</div>
                  <div className="text-right font-mono">
                    {seal.metrics.seal_success_rate != null
                      ? `${(seal.metrics.seal_success_rate * 100).toFixed(0)}%`
                      : '-'}
                  </div>
                </div>
                {seal.metrics.is_sealed === false && (
                  <div className="mt-1 text-[10px] text-amber-600 dark:text-amber-400">当前未封住</div>
                )}
              </div>
            )}

            {/* 暗盘拆单簇(数字流) */}
            {summary?.dark_clusters?.available && (
              <div className="border-b border-border/40 pb-2">
                <div className="text-[11px] text-muted-foreground">
                  暗盘拆单簇
                  <span className="ml-1 text-[10px] opacity-70">{summary.dark_clusters.cluster_count ?? 0} 个簇</span>
                </div>
                <div className="mt-1 grid grid-cols-2 gap-x-2 gap-y-0.5">
                  <div className="text-muted-foreground">主净额</div>
                  <div className={`text-right font-mono ${NET_INFLOW_CLASS(summary.dark_clusters.main_net)}`}>
                    {toWan(summary.dark_clusters.main_net)}
                  </div>
                  <div className="text-muted-foreground">暗盘</div>
                  <div className={`text-right font-mono ${NET_INFLOW_CLASS(summary.dark_clusters.dark_net)}`}>
                    {toWan(summary.dark_clusters.dark_net)}
                  </div>
                  <div className="text-muted-foreground">明盘</div>
                  <div className={`text-right font-mono ${NET_INFLOW_CLASS(summary.dark_clusters.ming_net)}`}>
                    {toWan(summary.dark_clusters.ming_net)}
                  </div>
                  <div className="text-muted-foreground">撤单率</div>
                  <div className="text-right font-mono">
                    {summary.dark_clusters.cancel_rate != null
                      ? `${(summary.dark_clusters.cancel_rate * 100).toFixed(1)}%`
                      : '-'}
                  </div>
                </div>
                {summary.dark_clusters.note && (
                  <div className="mt-1 text-[10px] text-muted-foreground/70">{summary.dark_clusters.note}</div>
                )}
              </div>
            )}

            {/* 关键事件(紧凑列表, P2-15: 收敛 SectionHeader) */}
            {normEvents.length > 0 && (
              <div className="border-b border-border/40 pb-2">
                <SectionHeader title="关键事件" />
                <ul className="mt-1 space-y-0.5">
                  {normEvents.slice(0, 6).map((e: KlineEventPoint, idx) => (
                    <li
                      key={`${e.time}-${idx}`}
                      className={`flex items-center gap-1.5 ${
                        hoveredDate && e.date.slice(0, 10) === hoveredDate
                          ? 'bg-primary/10 -mx-1 px-1 rounded'
                          : ''
                      }`}
                    >
                      <span className="text-[10px] text-muted-foreground font-mono w-16 shrink-0">
                        {e.date.slice(0, 10)}
                      </span>
                      <span className="text-foreground truncate flex-1" title={e.label}>
                        {e.icon ?? e.kind} {e.label}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* GS 末条信号(突出展示) */}
            {normGsSignals.length > 0 && (
              <div className="border-b border-border/40 pb-2">
                <div className="text-[11px] text-muted-foreground">GS 信号(末条)</div>
                <div
                  className={`mt-1 font-mono text-[13px] ${GS_COLOR(
                    normGsSignals[normGsSignals.length - 1].side,
                    normGsSignals[normGsSignals.length - 1].confirmed,
                  )}`}
                >
                  {normGsSignals[normGsSignals.length - 1].side === 'G' ? '● 买' : '○ 卖'}
                  {' '}
                  {normGsSignals[normGsSignals.length - 1].confirmed ? '已确认' : '待确认'}
                  <span className="ml-1.5 text-[10px] opacity-70 font-normal">
                    {normGsSignals[normGsSignals.length - 1].date}
                  </span>
                </div>
              </div>
            )}

            {/* 决策先锋共振(三指标共振状态, 替换原写死操作建议; 无卡片, hairline 分隔) */}
            <div className="border-b border-border/40 pb-2">
              <div className="text-[11px] text-muted-foreground">
                决策先锋共振
                {summary?.resonance?.available && summary.resonance.row != null && (
                  <span className="ml-1 font-mono text-[10px] opacity-70">第 {summary.resonance.row} 档</span>
                )}
              </div>
              {summary?.resonance?.available ? (
                <>
                  <div className="mt-1 flex items-baseline gap-1.5">
                    <span className={`font-mono text-[13px] font-semibold ${RESONANCE_TONE_CLASS(summary.resonance.tone)}`}>
                      {summary.resonance.action_label ?? '--'}
                    </span>
                    {summary.resonance.phase && summary.resonance.phase !== '无' && (
                      <span className={`text-[11px] ${RESONANCE_TONE_CLASS(summary.resonance.tone)}`}>
                        {summary.resonance.phase}
                      </span>
                    )}
                    {(summary.resonance.bad_count ?? 0) > 0 && (
                      <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                        {summary.resonance.bad_count} 项转坏
                      </span>
                    )}
                  </div>
                  {summary.resonance.action_text && (
                    <div className="mt-0.5 leading-snug text-foreground">{summary.resonance.action_text}</div>
                  )}
                  <div className="mt-0.5 text-[10px] text-muted-foreground/70">三指标共振 · 仅供参考</div>
                </>
              ) : (
                <div className="mt-1">
                  <span className="font-mono text-[13px] text-muted-foreground">观望</span>
                  <span className="ml-1.5 text-[10px] text-muted-foreground/70">共振数据缺失 · 不编造</span>
                </div>
              )}
            </div>
            {/* 2026-09-04 去空占位: 两项全空就不渲染整块(此前挂两个"--") */}
            {summary?.chips && (summary.chips.cost_10 != null || summary.chips.peak_price != null) ? (
              <div className="border-b border-border/40 pb-2">
                <div className="text-[11px] text-muted-foreground">筹码</div>
                <div className="mt-0.5 grid grid-cols-2 gap-x-2 font-mono">
                  {summary.chips.cost_10 != null && (
                    <>
                      <span className="text-muted-foreground">主力</span>
                      {/* P2-11: PG DECIMAL 序列化成字符串, Number() 防整页崩 */}
                      <span className="text-right">{Number(summary.chips.cost_10).toFixed(2)}</span>
                    </>
                  )}
                  {summary.chips.peak_price != null && (
                    <>
                      <span className="text-muted-foreground">峰价</span>
                      <span className="text-right">{Number(summary.chips.peak_price).toFixed(2)}</span>
                    </>
                  )}
                </div>
              </div>
            ) : null}
            {sourceHealth && typeof sourceHealth === "object" && Object.values(sourceHealth).some((s: any) => s === 'down') && (
              <div className="text-[10px] text-amber-500">
                ⚠ 部分数据源未连接（兜底数据可能缺失）
              </div>
            )}
          </div>
        </div>
      )}

      {/* === 数据明细(默认折叠) === */}
      <div className="border-t border-border/40 pt-2">
        <button
          type="button"
          onClick={() => setDetailOpen(!detailOpen)}
          className="flex w-full items-center gap-1 text-left text-[11px] text-muted-foreground hover:text-foreground"
        >
          <span>{detailOpen ? '▾' : '▸'}</span>
          <span>数据明细</span>
          {summary && (
            <span className="font-mono">
              · 资金 {summary.fund_flow?.length ?? 0} 行
              · 事件 {summary.events?.length ?? 0} 条
              {summary.dark_clusters?.available && ` · 暗盘簇 ${summary.dark_clusters.cluster_count ?? 0}`}
            </span>
          )}
        </button>
        {detailOpen && summary && (
          <div className="mt-2">
            <FundDetail
              rows={summary.fund_flow ?? []}
              hoveredDate={hoveredDate}
              selectedRange={selectedRange}
            />
          </div>
        )}
      </div>

      {/* === 预测(原 forecast Tab, 折叠入口) === */}
      {type === 'stock' && (
        <div className="border-t border-border/40 pt-2">
          <details>
            <summary className="cursor-pointer text-[11px] text-muted-foreground hover:text-foreground">
              预测(AI 4 模型)
            </summary>
            <div className="mt-2">
              <Suspense fallback={
                <div className="flex h-[20vh] items-center justify-center text-[12px] text-muted-foreground">
                  <Loader2 className="mr-2 h-3 w-3 animate-spin" /> 加载预测…
                </div>
              }>
                <ForecastPage />
              </Suspense>
            </div>
          </details>
        </div>
      )}
    </div>
  )
}

/** 数据明细折叠面板 — 资金流水表 */
function FundDetail({
  rows,
  hoveredDate,
  selectedRange,
}: {
  rows: FundFlowRow[]
  hoveredDate: string | null
  selectedRange: { from: string; to: string } | null
}) {
  const filtered = useMemo(() => {
    if (!selectedRange) return rows
    return rows.filter((r) => r.date >= selectedRange.from && r.date <= selectedRange.to)
  }, [rows, selectedRange])

  const recent = filtered.slice(-30).reverse()
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[11px]">
        <thead>
          <tr className="border-b border-border/40 text-muted-foreground">
            <th className="px-2 py-1 font-medium">日期</th>
            {/* P2-16: 两列区分明盘/暗盘净额 */}
            <th className="px-2 py-1 text-right font-medium">明盘净额(万/亿)</th>
            <th className="px-2 py-1 text-right font-medium">暗盘净额(万/亿)</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/40">
          {recent.map((r) => {
            const isHover = hoveredDate != null && r.date.slice(0, 10) === hoveredDate
            return (
              <tr
                key={r.date}
                className={`hover:bg-accent/20 ${isHover ? 'bg-accent/30' : ''}`}
              >
                <td className="px-2 py-1 font-mono text-muted-foreground">{r.date.slice(0, 10)}</td>
                <td className={`px-2 py-1 text-right font-mono ${NET_INFLOW_CLASS(r.ming_net)}`}>
                  {toWan(r.ming_net)}
                </td>
                <td className={`px-2 py-1 text-right font-mono ${NET_INFLOW_CLASS(r.dark_net)}`}>
                  {toWan(r.dark_net)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="mt-1 text-[10px] text-muted-foreground/70">
        显示最近 30 个交易日 · 红涨绿跌 · 区间由 K 线选段控制
      </div>
    </div>
  )
}