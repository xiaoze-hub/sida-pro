import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  insightApi,
  stocksApi,
  tradingAgentsApi,
  fundamentalsApi,
  type FundamentalsDetail,
  type DeepAnalysisResult,
  type HistoryComparisonResponse,
} from '@panwatch/api'
import { useLocalStorage } from '@/lib/utils'
import { useNavigate } from 'react-router-dom'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import type { MainIntentStructured } from '@panwatch/biz-ui/lib/main-intent-types'
import type { KlineSummary, SuggestionInfo } from '@panwatch/biz-ui/components/suggestion-badge'

/** KI-045: 本地归一(不依赖 @panwatch/api mock 面) —— 信封 `{items,degraded,note}` 或旧裸 list。 */
function normalizeNewsEnvelopeLocal<T = unknown>(raw: unknown): { items: T[]; degraded: boolean; note: string | null } {
  if (Array.isArray(raw)) return { items: raw as T[], degraded: false, note: null }
  if (raw && typeof raw === 'object' && Array.isArray((raw as { items?: unknown }).items)) {
    const o = raw as { items: T[]; degraded?: boolean; note?: string | null }
    return { items: o.items, degraded: !!o.degraded, note: o.note ?? null }
  }
  return { items: [], degraded: false, note: null }
}
import { normalizeSuggestionAction, pickSuggestionText, parseToMs } from './helpers'
import type {
  StockInsightModalProps,
  InsightTab,
  QuoteResponse,
  MoreInfoResponse,
  DarkFlowTqResponse,
  CompanyInfo,
  KlineSummaryResponse,
  SummaryOrderbook,
  GsSignalLike,
  FundFlowBarLike,
  KlineEventLike,
  MiniKlineResponse,
  NewsItem,
  HistoryRecord,
  PortfolioSummaryResponse,
  StockItem,
} from './types'

/**
 * 资源门控键(工作台 v2 三合一, Task 10)。spec §4.3: 进入工作台只取「带1+带2」所需的
 * `core` 端点, 下部标签各自的端点**只在该标签激活时**才取 —— 由宿主(各标签)按需传入
 * 自己的键集合实现。
 *
 * 语义: `keys === undefined` = **全开**(与不传该参数的旧行为逐字相同);
 *       `keys` 为空集 = **全关**(一个端点都不取, 自动刷新亦不启动)。
 *
 * 2026-09-13 解耦裁定(Task 13 复审): `deep`/`fundamentals`/`company` 三个键**只按键判定**,
 * **不再**与内部 `tab` 取与 —— 否则宿主只传 `keys` 时(工作台标签没有旧模态的标签栏, 内部
 * `tab` 恒为 `'overview'`)该键等于失效, 宿主只能 `setTab(...)` 代置共享的内部状态(会引起
 * `refreshForAuto` 等无关的 tab 分支副作用)。内部的 `tab` 仍保留给**旧模态语义**(自动刷新
 * 的按标签收敛)与恢复组件的 `setTab` 使用; 每个键的自动刷新判定见 `refreshForAuto`。
 */
export type ResourceKey =
  | 'core'          // 带1+带2: quote / moreInfo / darkFlowTq / klineSummary / klines(36d) / portfolioSummary
  | 'watchlist'     // stocksApi.list —— 关注状态
  | 'news'          // /news(含级联兜底)
  | 'announcements' // /news?source=eastmoney(含级联兜底)
  | 'suggestions'   // /suggestions
  | 'reports'       // /history x3 agent(含全局兜底)
  | 'deep'          // tradingAgents 深度分析(getLatestForStock + getHistoryComparison)
  | 'fundamentals'  // fundamentalsApi.detail(龙虎榜/两融/股东户数/分红/事件日历)
  | 'company'       // insightApi.company —— /quotes/{s}/company(公司简介/基本信息)

/** 单键判定: `keys` 缺省 = 全开。 */
export function isResourceEnabled(keys: ReadonlySet<ResourceKey> | undefined, key: ResourceKey): boolean {
  return !keys || keys.has(key)
}

/** 是否至少启用了一个键(`keys` 缺省 = 全开); 用于自动刷新总闸。 */
export function hasAnyResourceEnabled(keys: ReadonlySet<ResourceKey> | undefined): boolean {
  return !keys || keys.size > 0
}

export function useInsightData(props: StockInsightModalProps, enabledKeys?: ReadonlySet<ResourceKey>) {
const { toast } = useToast()
// W3.7/D7: 弹窗→全屏行情路由桥(弹窗预览 60%/侧栏场景的出口, 直达 /quote/:symbol)
const navigate = useNavigate()
const symbol = String(props.symbol || '').trim()
const goFullQuote = () => {
  props.onOpenChange(false)
  navigate(`/quote/${encodeURIComponent(symbol)}`)
}
const market = String(props.market || 'CN').trim().toUpperCase()
const [loading, setLoading] = useState(false)
const [tab, setTab] = useState<InsightTab>('overview')
const [newsHours, setNewsHours] = useLocalStorage<string>('stock_insight_news_hours', '168')
const [announcementHours, setAnnouncementHours] = useLocalStorage<string>('stock_insight_announcement_hours', '168')
const [includeExpiredSuggestions, setIncludeExpiredSuggestions] = useLocalStorage<boolean>(
  'stock_insight_include_expired_suggestions',
  true
)
const [autoRefreshEnabled, setAutoRefreshEnabled] = useLocalStorage<boolean>(
  'stock_insight_auto_refresh_enabled',
  true
)
const [autoRefreshSec, setAutoRefreshSec] = useLocalStorage<number>(
  'stock_insight_auto_refresh_sec',
  20
)
const [quote, setQuote] = useState<QuoteResponse | null>(null)
const [moreInfo, setMoreInfo] = useState<MoreInfoResponse | null>(null)
const [moreInfoLoading, setMoreInfoLoading] = useState(false)
const [darkFlowTq, setDarkFlowTq] = useState<DarkFlowTqResponse | null>(null)
const [companyInfo, setCompanyInfo] = useState<CompanyInfo | null>(null)
const [companyLoading, setCompanyLoading] = useState(false)
// 基本面明细(龙虎榜/股东户数/分红/两融/事件日历): 懒加载 + 静默降级
const [fundamentals, setFundamentals] = useState<FundamentalsDetail | null>(null)
const [fundamentalsLoading, setFundamentalsLoading] = useState(false)
const [fundamentalsLoaded, setFundamentalsLoaded] = useState(false)
const [klineSummary, setKlineSummary] = useState<KlineSummary | null>(null)
/**
 * `GET /klines/{s}/summary` 的**顶层** `orderbook`(盘口形态/最优买卖/价差/买盘占比)。
 * v0.6.0 遗留④: 此前 `loadKline` 只存 `data.summary`(⇒ `klineSummary` 里没有 `orderbook`),
 * 消费方(工作台「盘口资金」标签)拿不到退役 `/l2` 页原本渲染的那四个盘口读数, 只能用
 * `/orderbook-ob` 的 OB 序列 label 当形态代理。现单独存一份并暴露(缺 → `null`, 由渲染层走 `--`)。
 */
const [summaryOrderbook, setSummaryOrderbook] = useState<SummaryOrderbook | null>(null)
/** 2026-08-12 预热优化: 弹窗打开即拉主力意图, 切到 K线 tab 秒显图例卡 */
const [mainIntent, setMainIntent] = useState<MainIntentStructured | null>(null)
// ============== SIDA Pro: K线图层标注数据 state (P1+ P2) (2026-09-01) ==============
const [gsSignals, setGsSignals] = useState<GsSignalLike[]>([])
const [fundFlow, setFundFlow] = useState<FundFlowBarLike[]>([])
const [klineEvents, setKlineEvents] = useState<KlineEventLike[]>([])
const [miniKlines, setMiniKlines] = useState<MiniKlineResponse['klines']>([])
const [miniKlineLoading, setMiniKlineLoading] = useState(false)
const [miniHoverIdx, setMiniHoverIdx] = useState<number | null>(null)
const [suggestions, setSuggestions] = useState<SuggestionInfo[]>([])
const [news, setNews] = useState<NewsItem[]>([])
const [announcements, setAnnouncements] = useState<NewsItem[]>([])
const [reports, setReports] = useState<HistoryRecord[]>([])
const [reportTab, setReportTab] = useState<'premarket_outlook' | 'daily_report' | 'news_digest'>('premarket_outlook')
const [deepResult, setDeepResult] = useState<DeepAnalysisResult | null>(null)
const [deepLoading, setDeepLoading] = useState(false)
const [deepLoaded, setDeepLoaded] = useState(false)
const [deepShowAnalyst, setDeepShowAnalyst] = useState(false)
const [deepShowDebate, setDeepShowDebate] = useState(false)
const [deepHistory, setDeepHistory] = useState<HistoryComparisonResponse | null>(null)
const [deepHistoryLoading, setDeepHistoryLoading] = useState(false)
const [klineInterval] = useState<'1d' | '1w' | '1m'>('1d')
const [alerting, setAlerting] = useState(false)
const [watchingStock, setWatchingStock] = useState<StockItem | null>(null)
const [watchToggleLoading, setWatchToggleLoading] = useState(false)
const [autoSuggesting, setAutoSuggesting] = useState(false)
const [imageExporting, setImageExporting] = useState(false)
const [holdingAgg, setHoldingAgg] = useState<{
  quantity: number
  cost: number
  unitCost: number
  marketValue: number
  pnl: number
} | null>(null)
const [holdingLoaded, setHoldingLoaded] = useState(false)
const [holdingLoadError, setHoldingLoadError] = useState(false)
const autoTriggeredRef = useRef<Record<string, number>>({})
const stockCacheRef = useRef<Record<string, StockItem>>({})
const resolvedName = useMemo(() => props.stockName || quote?.name || symbol, [props.stockName, quote?.name, symbol])

const loadQuote = useCallback(async () => {
  if (!symbol) return
  const data = await insightApi.quote<QuoteResponse>(symbol, market)
  setQuote(data || null)
}, [symbol, market])

const loadMoreInfo = useCallback(async () => {
  if (!symbol) return
  setMoreInfoLoading(true)
  try {
    const data = await insightApi.moreInfo<MoreInfoResponse>(symbol, market)
    setMoreInfo(data || null)
  } catch {
    setMoreInfo(null)
  } finally {
    setMoreInfoLoading(false)
  }
}, [symbol, market])

const loadDarkFlowTq = useCallback(async () => {
  if (!symbol) return
  try {
    const data = await insightApi.darkFlowTq<DarkFlowTqResponse>(symbol, market)
    setDarkFlowTq(data || null)
  } catch {
    setDarkFlowTq(null) // 404 = 盘后未采集, 静默降级
  }
}, [symbol, market])

const loadCompany = useCallback(async () => {
  if (!symbol || companyInfo) return
  setCompanyLoading(true)
  try {
    const data = await insightApi.company<CompanyInfo>(symbol, market)
    setCompanyInfo(data || null)
  } catch {
    setCompanyInfo(null)
  } finally {
    setCompanyLoading(false)
  }
}, [symbol, market, companyInfo])

/**
 * 拉取基本面明细(龙虎榜/股东户数/分红/融资融券/事件日历)。
 * 该端点后端可能尚未就绪(404/超时): 失败静默降级, 不 toast、不阻断弹窗其他功能。
 */
const loadFundamentals = useCallback(async () => {
  if (!symbol) return
  setFundamentalsLoading(true)
  try {
    const data = await fundamentalsApi.detail(symbol, market)
    setFundamentals(data || null)
  } catch {
    setFundamentals(null)
  } finally {
    setFundamentalsLoaded(true)
    setFundamentalsLoading(false)
  }
}, [symbol, market])

const loadKline = useCallback(async () => {
  if (!symbol) return
  const data = await insightApi.klineSummary<KlineSummaryResponse>(symbol, market)
  setKlineSummary(data?.summary || null)
  // 遗留④: 顶层 `orderbook`(与 summary 平级)单独存一份 —— 无条件 set(缺失即 null),
  // 否则换标的/源不可用时会把上一只票的盘口形态留在屏上。
  setSummaryOrderbook(data?.orderbook ?? null)
  // 2026-08-12 预热优化: 顺手存主力意图, 传给 K线 tab 秒显(免组件二次请求)
  if (data?.main_intent_structured) setMainIntent(data.main_intent_structured)
  // ============== SIDA Pro: K线图层标注数据 (P1+ P2 入口) (2026-09-01) ==============
  // 后端 P2 阶段输出这些字段, 未就绪时为 undefined → K线组件按 layers 默认开关静默跳过
  if (data?.gs_signals) setGsSignals(data.gs_signals)
  if (data?.fund_flow) setFundFlow(data.fund_flow)
  if (data?.events) setKlineEvents(data.events)
}, [symbol, market])

const loadMiniKline = useCallback(async (opts?: { silent?: boolean }) => {
  if (!symbol) return
  const silent = !!opts?.silent
  if (!silent) setMiniKlineLoading(true)
  try {
    const data = await insightApi.klines<MiniKlineResponse>(symbol, {
      market,
      days: 36,
      interval: '1d',
    })
    setMiniKlines((data?.klines || []).slice(-30))
  } catch {
    setMiniKlines([])
  } finally {
    if (!silent) setMiniKlineLoading(false)
  }
}, [symbol, market])

const loadSuggestions = useCallback(async () => {
  if (!symbol) return
  const data = await insightApi.suggestions<any[]>(symbol, {
    market,
    limit: 20,
    include_expired: includeExpiredSuggestions,
  })
  const list = (data || []).map(item => ({
    id: item.id,
    action: normalizeSuggestionAction(item.action, item.action_label),
    action_label: item.action_label || '',
    signal: pickSuggestionText(item.signal, 'signal'),
    reason: pickSuggestionText(item.reason, 'reason'),
    should_alert: !!item.should_alert,
    agent_name: item.agent_name,
    agent_label: item.agent_label,
    created_at: item.created_at,
    is_expired: item.is_expired,
    prompt_context: item.prompt_context,
    ai_response: item.ai_response,
    raw: item.raw || '',
    meta: item.meta,
  })) as SuggestionInfo[]
  setSuggestions(list)
}, [symbol, market, includeExpiredSuggestions])

const loadNews = useCallback(async () => {
  if (!symbol) return
  const runQuery = async (opts: { useName: boolean; filterRelated: boolean }) => {
    const params = new URLSearchParams()
    params.set('hours', newsHours)
    params.set('limit', '50')
    if (!opts.filterRelated) params.set('filter_related', 'false')
    if (opts.useName && resolvedName && resolvedName !== symbol) params.set('names', resolvedName)
    else params.set('symbols', symbol)
    const raw = await insightApi.news<unknown>(Object.fromEntries(params.entries()))
    return normalizeNewsEnvelopeLocal<NewsItem>(raw).items
  }

  try {
    let data: NewsItem[] = await runQuery({ useName: true, filterRelated: true })
    if ((data || []).length === 0 && resolvedName && resolvedName !== symbol) {
      data = await runQuery({ useName: false, filterRelated: true })
    }
    if ((data || []).length === 0) {
      data = await runQuery({ useName: true, filterRelated: false })
    }
    if ((data || []).length === 0) {
      data = await runQuery({ useName: false, filterRelated: false })
    }
    if ((data || []).length === 0) {
      const raw = await insightApi.news<unknown>({
        hours: newsHours,
        limit: 80,
      }).catch(() => [])
      const global = normalizeNewsEnvelopeLocal<NewsItem>(raw).items
      const upperSymbol = symbol.toUpperCase()
      const name = (resolvedName || '').trim()
      data = (global || []).filter((n) => {
        const text = `${n.title || ''} ${n.content || ''}`.toUpperCase()
        if (upperSymbol && text.includes(upperSymbol)) return true
        if (name && `${n.title || ''} ${n.content || ''}`.includes(name)) return true
        return (n.symbols || []).map(x => String(x).toUpperCase()).includes(upperSymbol)
      })
    }
    // 兜底：实时新闻为空时，回退到 news_digest 历史快照中的新闻列表
    if ((data || []).length === 0) {
      const bySymbol = await insightApi.history<HistoryRecord[]>({
        agent_name: 'news_digest',
        stock_symbol: symbol,
        limit: 1,
      }).catch(() => [])
      let rec: HistoryRecord | null = (bySymbol || [])[0] || null
      if (!rec) {
        const globals = await insightApi.history<HistoryRecord[]>({
          agent_name: 'news_digest',
          stock_symbol: '*',
          limit: 20,
        }).catch(() => [])
        const upperSymbol = symbol.toUpperCase()
        const name = (resolvedName || '').trim()
        rec = (globals || []).find((r) => {
          const sug = r?.suggestions || {}
          const keys = Object.keys(sug || {})
          if (keys.includes(symbol) || keys.map(k => k.toUpperCase()).includes(upperSymbol)) return true
          const text = `${r?.title || ''}\n${r?.content || ''}`.toUpperCase()
          if (upperSymbol && text.includes(upperSymbol)) return true
          if (name && `${r?.title || ''}\n${r?.content || ''}`.includes(name)) return true
          return false
        }) || null
      }
      if (rec?.news && Array.isArray(rec.news)) {
        data = rec.news
          .map((n) => ({
            source: n.source || 'news_digest',
            source_label: n.source || 'news_digest',
            title: n.title || '',
            publish_time: n.publish_time || rec?.analysis_date || '',
            url: n.url || '',
          }))
          .filter((n) => !!n.title)
      }
    }
    setNews(data || [])
  } catch {
    setNews([])
  }
}, [symbol, newsHours, resolvedName])

const loadAnnouncements = useCallback(async () => {
  if (!symbol) return
  try {
    const runQuery = async (opts: { useName: boolean; filterRelated: boolean }) => {
      const params = new URLSearchParams()
      params.set('hours', announcementHours)
      params.set('limit', '50')
      if (!opts.filterRelated) params.set('filter_related', 'false')
      params.set('source', 'eastmoney')
      if (opts.useName && resolvedName && resolvedName !== symbol) params.set('names', resolvedName)
      else params.set('symbols', symbol)
      const raw = await insightApi.news<unknown>(Object.fromEntries(params.entries()))
      return normalizeNewsEnvelopeLocal<NewsItem>(raw).items
    }
    let data: NewsItem[] = await runQuery({ useName: true, filterRelated: true })
    if ((data || []).length === 0 && resolvedName && resolvedName !== symbol) {
      data = await runQuery({ useName: false, filterRelated: true })
    }
    if ((data || []).length === 0) {
      data = await runQuery({ useName: true, filterRelated: false })
    }
    if ((data || []).length === 0) {
      data = await runQuery({ useName: false, filterRelated: false })
    }
    if ((data || []).length === 0) {
      const raw = await insightApi.news<unknown>({
        hours: announcementHours,
        limit: 80,
        source: 'eastmoney',
      }).catch(() => [])
      const global = normalizeNewsEnvelopeLocal<NewsItem>(raw).items
      const upperSymbol = symbol.toUpperCase()
      const name = (resolvedName || '').trim()
      data = (global || []).filter((n) => {
        const text = `${n.title || ''} ${n.content || ''}`.toUpperCase()
        if (upperSymbol && text.includes(upperSymbol)) return true
        if (name && `${n.title || ''} ${n.content || ''}`.includes(name)) return true
        return (n.symbols || []).map(x => String(x).toUpperCase()).includes(upperSymbol)
      })
    }
    setAnnouncements(data || [])
  } catch {
    setAnnouncements([])
  }
}, [symbol, announcementHours, resolvedName])

const loadHoldingAgg = useCallback(async () => {
  if (!symbol) return
  setHoldingLoaded(false)
  setHoldingLoadError(false)
  try {
    const data = await insightApi.portfolioSummary<PortfolioSummaryResponse>({ include_quotes: true })
    let quantity = 0
    let cost = 0
    let marketValue = 0
    let pnl = 0
    for (const acc of data?.accounts || []) {
      for (const p of acc.positions || []) {
        if (p.symbol !== symbol || String(p.market || '').trim().toUpperCase() !== market) continue
        quantity += Number(p.quantity || 0)
        cost += Number(p.cost_price || 0) * Number(p.quantity || 0)
        marketValue += Number(p.market_value_cny || 0)
        pnl += Number(p.pnl || 0)
      }
    }
    if (quantity > 0) setHoldingAgg({ quantity, cost, unitCost: cost / quantity, marketValue, pnl })
    else setHoldingAgg(null)
  } catch {
    setHoldingAgg(null)
    setHoldingLoadError(true)
  } finally {
    setHoldingLoaded(true)
  }
}, [symbol, market])

const loadReports = useCallback(async () => {
  if (!symbol) return
  try {
    const agents = ['premarket_outlook', 'daily_report', 'news_digest']
    const bySymbolResults = await Promise.all(
      agents.map(agent =>
        insightApi.history<HistoryRecord[]>({
          agent_name: agent,
          stock_symbol: symbol,
          limit: 1,
        }).catch(() => [])
      )
    )
    let merged = bySymbolResults
      .flatMap(items => items || [])
      .filter(Boolean)
    // 兼容全局记录（stock_symbol="*"）场景：从最近全局记录中筛选与当前股票相关的报告。
    if (merged.length === 0) {
      const globalResults = await Promise.all(
        agents.map(agent =>
          insightApi.history<HistoryRecord[]>({
            agent_name: agent,
            stock_symbol: '*',
            limit: 20,
          }).catch(() => [])
        )
      )
      const upperSymbol = symbol.toUpperCase()
      const name = (resolvedName || '').trim()
      merged = globalResults
        .map(items => {
          const rows = (items || []).filter(Boolean)
          const hit = rows.find((r) => {
            const sug = r?.suggestions || {}
            const keys = Object.keys(sug || {})
            if (keys.includes(symbol) || keys.map(k => k.toUpperCase()).includes(upperSymbol)) return true
            const text = `${r?.title || ''}\n${r?.content || ''}`.toUpperCase()
            if (upperSymbol && text.includes(upperSymbol)) return true
            if (name && `${r?.title || ''}\n${r?.content || ''}`.includes(name)) return true
            return false
          })
          return hit || null
        })
        .filter(Boolean) as HistoryRecord[]
    }
    merged = merged.sort((a, b) => {
      const am = parseToMs(a.updated_at || a.created_at || a.analysis_date) || 0
      const bm = parseToMs(b.updated_at || b.created_at || b.analysis_date) || 0
      return bm - am
    })
    setReports(merged)
  } catch {
    setReports([])
  }
}, [symbol, resolvedName])

const loadCore = useCallback(async () => {
  if (!symbol) return
  setLoading(true)
  try {
    await Promise.allSettled([loadQuote(), loadMoreInfo(), loadDarkFlowTq(), loadKline(), loadMiniKline(), loadHoldingAgg()])
  } catch (e) {
    toast(e instanceof Error ? e.message : '加载失败', 'error')
  } finally {
    setLoading(false)
  }
}, [symbol, loadQuote, loadMoreInfo, loadDarkFlowTq, loadKline, loadMiniKline, loadHoldingAgg, toast])

const handleRefreshAll = useCallback(async () => {
  if (!symbol) return
  setLoading(true)
  try {
    await Promise.allSettled([loadQuote(), loadMoreInfo(), loadDarkFlowTq(), loadKline(), loadMiniKline(), loadSuggestions(), loadNews(), loadAnnouncements(), loadHoldingAgg(), loadReports(), loadFundamentals()])
  } catch (e) {
    toast(e instanceof Error ? e.message : '加载失败', 'error')
  } finally {
    setLoading(false)
  }
}, [symbol, loadQuote, loadMoreInfo, loadDarkFlowTq, loadKline, loadMiniKline, loadSuggestions, loadNews, loadAnnouncements, loadHoldingAgg, loadReports, loadFundamentals, toast])

const refreshForAuto = useCallback(async () => {
  if (!symbol) return
  const tasks: Promise<any>[] = []
  // 键门控(缺省 undefined = 全开)。**已不再按内部 `tab` 收敛** —— 唯一的 `setTab` 调用方是
  // `OverviewTab`, 而该组件随旧模态一起删除(2026-09-13, 全仓零引用) ⇒ 所有消费者的 `tab` 恒为
  // `'overview'`, 保留 `tab === 'X'` 分支只会让"什么会随 tick 重取"难以读懂(遗留⑪: 清掉这批已死分支;
  // 在当前消费者上**行为零变化** —— 原来 `tab==='overview'` 就已命中 kline/suggestions/news/announcements/reports)。
  if (isResourceEnabled(enabledKeys, 'core')) {
    tasks.push(loadQuote(), loadMoreInfo(), loadHoldingAgg(), loadKline(), loadMiniKline({ silent: true }))
  }
  if (isResourceEnabled(enabledKeys, 'suggestions')) {
    tasks.push(loadSuggestions())
  }
  if (isResourceEnabled(enabledKeys, 'news')) {
    tasks.push(loadNews())
  }
  if (isResourceEnabled(enabledKeys, 'announcements')) {
    tasks.push(loadAnnouncements())
  }
  if (isResourceEnabled(enabledKeys, 'reports')) {
    tasks.push(loadReports())
  }
  // **EOD 数据有意不随 tick 重取**: `company`(简介/基本信息) 与 `fundamentals`(龙虎榜/两融/股东户数)
  // 无盘内新鲜度承诺, 20s 重取纯浪费配额 —— 需要重取时走 Provider 暴露的 `handleRefreshAll`(手动刷新)。
  await Promise.allSettled(tasks)
}, [symbol, enabledKeys, loadQuote, loadMoreInfo, loadHoldingAgg, loadKline, loadMiniKline, loadSuggestions, loadNews, loadAnnouncements, loadReports])

const loadDeepResult = useCallback(async () => {
  if (!symbol) return
  setDeepLoading(true)
  setDeepHistoryLoading(true)
  try {
    const [latest, history] = await Promise.allSettled([
      tradingAgentsApi.getLatestForStock(symbol),
      tradingAgentsApi.getHistoryComparison(symbol, market, 90),
    ])
    setDeepResult(latest.status === 'fulfilled' ? latest.value : null)
    setDeepHistory(history.status === 'fulfilled' ? history.value : null)
  } catch {
    setDeepResult(null)
    setDeepHistory(null)
  } finally {
    setDeepLoaded(true)
    setDeepLoading(false)
    setDeepHistoryLoading(false)
  }
}, [symbol, market])

useEffect(() => {
  if (!props.open || !symbol) return
  // Task 10 门控: `core` = 带1+带2 所需的全部端点; 未启用则连状态重置也跳过(每次挂载都是新实例)。
  if (!isResourceEnabled(enabledKeys, 'core')) return
  setTab('overview')
  setSuggestions([])
  setNews([])
  setAnnouncements([])
  setReports([])
  setMiniKlines([])
  setWatchingStock(null)
  setDeepResult(null)
  setDeepLoaded(false)
  setDeepHistory(null)
  setFundamentals(null)
  setFundamentalsLoaded(false)
  setMoreInfo(null)
  // 换标的清盘口形态(遗留④): 顶行/快照同理, 不许把上一只票的 orderbook 画到新标的上
  setSummaryOrderbook(null)
  loadCore()
}, [props.open, symbol, market, enabledKeys, loadCore])

// `deep` 键启用即取(**只按键判定**, 不再要求内部 tab==='deep' —— 见 ResourceKey 头注):
// 宿主(工作台「研究/预测」等标签)没有旧模态标签栏, 内部 `tab` 恒为 'overview'。
useEffect(() => {
  if (!props.open || !symbol) return
  if (!isResourceEnabled(enabledKeys, 'deep')) return
  if (!deepLoaded && !deepLoading) {
    loadDeepResult()
  }
}, [props.open, symbol, enabledKeys, deepLoaded, deepLoading, loadDeepResult])

// `fundamentals` 键启用即取(**只按键判定**, 不再要求内部 tab==='fundamentals' —— 见 ResourceKey
// 头注; Task 13 复审裁定)。失败也置 loaded, 避免反复请求 404。
useEffect(() => {
  if (!props.open || !symbol) return
  if (!isResourceEnabled(enabledKeys, 'fundamentals')) return
  if (!fundamentalsLoaded && !fundamentalsLoading) {
    loadFundamentals()
  }
}, [props.open, symbol, enabledKeys, fundamentalsLoaded, fundamentalsLoading, loadFundamentals])

// `company` 键启用即取(Task 13 复审新增的键 —— 此前 `/quotes/{s}/company` **没有任何键**,
// 只由 `refreshForAuto` 的 `tab === 'company'` 分支触发 ⇒ 无旧模态标签栏的宿主只能直调
// `loadCompany()`; 现在按键门控, 与其它键同形)。幂等由 `loadCompany` 自身的
// `if (companyInfo) return` 早退保证(成功后 `companyInfo` 变化 → 本 effect 重跑 → 早退,
// 不再发请求也不改状态 ⇒ 无循环); 换标的由宿主的 `key={symbol}` 重挂载保证。
useEffect(() => {
  if (!props.open || !symbol) return
  if (!isResourceEnabled(enabledKeys, 'company')) return
  loadCompany()
}, [props.open, symbol, enabledKeys, loadCompany])

useEffect(() => {
  if (!props.open || !symbol) return
  if (!isResourceEnabled(enabledKeys, 'watchlist')) return
  let cancelled = false
  ;(async () => {
    try {
      const key = `${market}:${symbol}`
      const stocks = await stocksApi.list()
      if (cancelled) return
      const found = (stocks || []).find(s => s.symbol === symbol && s.market === market) || null
      if (found) {
        stockCacheRef.current[key] = found
      } else {
        delete stockCacheRef.current[key]
      }
      setWatchingStock(found)
    } catch {
      if (!cancelled) setWatchingStock(null)
    }
  })()
  return () => { cancelled = true }
}, [props.open, symbol, market, enabledKeys])

useEffect(() => {
  if (!props.open || !symbol) return
  if (!isResourceEnabled(enabledKeys, 'news')) return
  loadNews().catch(() => setNews([]))
}, [props.open, symbol, newsHours, enabledKeys, loadNews])

useEffect(() => {
  if (!props.open || !symbol) return
  if (!isResourceEnabled(enabledKeys, 'announcements')) return
  loadAnnouncements().catch(() => setAnnouncements([]))
}, [props.open, symbol, announcementHours, enabledKeys, loadAnnouncements])

useEffect(() => {
  if (!props.open || !symbol) return
  if (!isResourceEnabled(enabledKeys, 'suggestions')) return
  loadSuggestions().catch(() => setSuggestions([]))
}, [props.open, symbol, includeExpiredSuggestions, enabledKeys, loadSuggestions])

useEffect(() => {
  if (!props.open || !symbol) return
  if (!isResourceEnabled(enabledKeys, 'reports')) return
  loadReports().catch(() => setReports([]))
}, [props.open, symbol, enabledKeys, loadReports])

useEffect(() => {
  if (!props.open || !symbol || !autoRefreshEnabled) return
  // Task 10 门控: 一个键都没启用时, 20s 自动刷新整体不启动(缺省全开 = 旧行为不变)。
  if (!hasAnyResourceEnabled(enabledKeys)) return
  const sec = Number(autoRefreshSec) > 0 ? Number(autoRefreshSec) : 20
  const ms = Math.max(10, sec) * 1000
  const timer = setInterval(() => {
    refreshForAuto().catch(() => undefined)
  }, ms)
  return () => clearInterval(timer)
}, [props.open, symbol, autoRefreshEnabled, autoRefreshSec, enabledKeys, refreshForAuto])

const miniKlineExtrema = useMemo(() => {
  if (!miniKlines.length) return null
  let low = Number.POSITIVE_INFINITY
  let high = Number.NEGATIVE_INFINITY
  for (const k of miniKlines) {
    low = Math.min(low, Number(k.low))
    high = Math.max(high, Number(k.high))
  }
  if (!isFinite(low) || !isFinite(high) || high <= low) return null
  return { low, high }
}, [miniKlines])

  return {
    toast,
    navigate,
    symbol,
    goFullQuote,
    market,
    loading,
    setLoading,
    tab,
    setTab,
    newsHours,
    setNewsHours,
    announcementHours,
    setAnnouncementHours,
    includeExpiredSuggestions,
    setIncludeExpiredSuggestions,
    autoRefreshEnabled,
    setAutoRefreshEnabled,
    autoRefreshSec,
    setAutoRefreshSec,
    quote,
    moreInfo,
    moreInfoLoading,
    darkFlowTq,
    companyInfo,
    companyLoading,
    fundamentals,
    fundamentalsLoading,
    fundamentalsLoaded,
    klineSummary,
    // 遗留④: `/klines/{s}/summary` 的**顶层** orderbook(形态/最优买卖/价差/买盘占比)
    summaryOrderbook,
    mainIntent,
    gsSignals,
    fundFlow,
    klineEvents,
    miniKlines,
    miniKlineLoading,
    miniHoverIdx,
    setMiniHoverIdx,
    suggestions,
    news,
    announcements,
    reports,
    reportTab,
    setReportTab,
    deepResult,
    deepLoading,
    deepLoaded,
    deepShowAnalyst,
    setDeepShowAnalyst,
    deepShowDebate,
    setDeepShowDebate,
    deepHistory,
    deepHistoryLoading,
    klineInterval,
    alerting,
    setAlerting,
    watchingStock,
    setWatchingStock,
    watchToggleLoading,
    setWatchToggleLoading,
    autoSuggesting,
    setAutoSuggesting,
    imageExporting,
    setImageExporting,
    holdingAgg,
    holdingLoaded,
    holdingLoadError,
    autoTriggeredRef,
    stockCacheRef,
    resolvedName,
    loadQuote,
    loadMoreInfo,
    loadDarkFlowTq,
    loadCompany,
    loadFundamentals,
    loadKline,
    loadMiniKline,
    loadSuggestions,
    loadNews,
    loadAnnouncements,
    loadHoldingAgg,
    loadReports,
    loadCore,
    handleRefreshAll,
    refreshForAuto,
    loadDeepResult,
    miniKlineExtrema,
  }
}
