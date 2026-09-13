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
import type { MainIntentStructured } from '@panwatch/biz-ui/components/InteractiveKline'
import type { KlineSummary, SuggestionInfo } from '@panwatch/biz-ui/components/suggestion-badge'
import { normalizeSuggestionAction, pickSuggestionText, parseToMs } from './helpers'
import type {
  StockInsightModalProps,
  InsightTab,
  QuoteResponse,
  MoreInfoResponse,
  DarkFlowTqResponse,
  CompanyInfo,
  KlineSummaryResponse,
  GsSignalLike,
  FundFlowBarLike,
  KlineEventLike,
  MiniKlineResponse,
  NewsItem,
  HistoryRecord,
  PortfolioSummaryResponse,
  StockItem,
} from './types'

export function useInsightData(props: StockInsightModalProps) {
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
    return insightApi.news<NewsItem[]>(Object.fromEntries(params.entries()))
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
      const global = await insightApi.news<NewsItem[]>({
        hours: newsHours,
        limit: 80,
      }).catch(() => [])
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
      return insightApi.news<NewsItem[]>(Object.fromEntries(params.entries()))
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
      const global = await insightApi.news<NewsItem[]>({
        hours: announcementHours,
        limit: 80,
        source: 'eastmoney',
      }).catch(() => [])
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
        if (p.symbol !== symbol || p.market !== market) continue
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
  const tasks: Promise<any>[] = [loadQuote(), loadMoreInfo(), loadHoldingAgg()]
  if (tab === 'overview' || tab === 'kline') {
    tasks.push(loadKline(), loadMiniKline({ silent: true }))
  }
  if (tab === 'overview' || tab === 'suggestions') {
    tasks.push(loadSuggestions())
  }
  if (tab === 'overview' || tab === 'news') {
    tasks.push(loadNews())
  }
  if (tab === 'overview' || tab === 'announcements') {
    tasks.push(loadAnnouncements())
  }
  if (tab === 'overview' || tab === 'reports') {
    tasks.push(loadReports())
  }
  if (tab === 'company') {
    tasks.push(loadCompany())
  }
  if (tab === 'fundamentals') {
    tasks.push(loadFundamentals())
  }
  await Promise.allSettled(tasks)
}, [symbol, tab, loadQuote, loadMoreInfo, loadHoldingAgg, loadKline, loadMiniKline, loadSuggestions, loadNews, loadAnnouncements, loadReports, loadCompany, loadFundamentals])

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
  loadCore()
}, [props.open, symbol, market, loadCore])

// 切到「深度」tab 时按需拉取(仅首次)
useEffect(() => {
  if (!props.open || !symbol) return
  if (tab === 'deep' && !deepLoaded && !deepLoading) {
    loadDeepResult()
  }
}, [tab, props.open, symbol, deepLoaded, deepLoading, loadDeepResult])

// 切到「基本面」tab 时按需拉取(仅首次; 失败也置 loaded, 避免反复请求 404)
useEffect(() => {
  if (!props.open || !symbol) return
  if (tab === 'fundamentals' && !fundamentalsLoaded && !fundamentalsLoading) {
    loadFundamentals()
  }
}, [tab, props.open, symbol, fundamentalsLoaded, fundamentalsLoading, loadFundamentals])

useEffect(() => {
  if (!props.open || !symbol) return
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
}, [props.open, symbol, market])

useEffect(() => {
  if (!props.open || !symbol) return
  loadNews().catch(() => setNews([]))
}, [props.open, symbol, newsHours, loadNews])

useEffect(() => {
  if (!props.open || !symbol) return
  loadAnnouncements().catch(() => setAnnouncements([]))
}, [props.open, symbol, announcementHours, loadAnnouncements])

useEffect(() => {
  if (!props.open || !symbol) return
  loadSuggestions().catch(() => setSuggestions([]))
}, [props.open, symbol, includeExpiredSuggestions, loadSuggestions])

useEffect(() => {
  if (!props.open || !symbol) return
  loadReports().catch(() => setReports([]))
}, [props.open, symbol, loadReports])

useEffect(() => {
  if (!props.open || !symbol || !autoRefreshEnabled) return
  const sec = Number(autoRefreshSec) > 0 ? Number(autoRefreshSec) : 20
  const ms = Math.max(10, sec) * 1000
  const timer = setInterval(() => {
    refreshForAuto().catch(() => undefined)
  }, ms)
  return () => clearInterval(timer)
}, [props.open, symbol, autoRefreshEnabled, autoRefreshSec, refreshForAuto])

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
