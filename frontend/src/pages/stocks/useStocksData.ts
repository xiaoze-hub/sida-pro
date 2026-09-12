import { useEffect, useRef, useCallback } from 'react'
import type { AIService } from '@panwatch/api'
import type { Account } from './shared'
import type { AgentConfig } from './shared'
import type { KlineSummary } from '@panwatch/biz-ui/components/suggestion-badge'
import type { MarketStatus } from './shared'
import type { NewsItem } from './shared'
import type { NotifyChannel } from '@panwatch/api'
import type { PoolSuggestion } from './shared'
import type { PortfolioSummary } from './shared'
import type { Position } from './shared'
import type { PriceAlertRuleSummary } from './shared'
import type { QuoteRequestItem } from './shared'
import type { QuoteResponse } from './shared'
import type { QuoteSnapshot } from './shared'
import type { SchedulePreview } from './shared'
import type { Stock } from './shared'
import type { StockAgentInfo } from './shared'
import type { StockContextTarget } from '@/components/StockContextMenu'
import { fetchAPI } from '@panwatch/api'
import { useQuoteStream, type QuoteTickMap } from '@/realtime/useQuoteStream'
import { mergePortfolioQuotes } from './shared'
import { parseServerTime } from '@/lib/utils'
import { stocksApi } from '@panwatch/api'
import { useNavigate } from 'react-router-dom'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import type { useStocksState } from './useStocksState'

export function useStocksData(state: ReturnType<typeof useStocksState>) {
  const {
    stocks,
    setStocks,
    setAccounts,
    agents,
    setAgents,
    setServices,
    setChannels,
    setLoading,
    setLoadError,
    portfolio,
    setPortfolio,
    portfolioRaw,
    setPortfolioRaw,
    setPortfolioLoading,
    setExpandedAccounts,
    quotes,
    setQuotes,
    setQuotesLoading,
    klineSummaries,
    setKlineSummaries,
    autoRefresh,
    refreshInterval,
    setLastRefreshTime,
    refreshTimerRef,
    setScanning,
    setPoolSuggestions,
    setPoolSuggestionsLoading,
    setPriceAlertSummaryMap,
    setNewsDialogOpen,
    setNewsDialogSymbol,
    setNews,
    setNewsLoading,
    setKlineDialogOpen,
    setKlineDialogSymbol,
    setKlineDialogMarket,
    setKlineDialogName,
    setKlineDialogHasPosition,
    setKlineDialogInitialSummary,
    setMinuteDialogOpen,
    setMinuteDialogSymbol,
    setMinuteDialogMarket,
    setMinuteDialogName,
    setStockCtxMenu,
    setMarketStatus,
    klineRefreshInFlight,
    agentDialogStock,
    setDeepAnalysisTarget,
    schedulePreviewCache,
    setSchedulePreviewCache,
    schedulePreviewLoading,
    setSchedulePreviewLoading,
    watchDragSnapshotRef,
    positionDragSnapshotRef,
  } = state
const openDeepAnalysis = useCallback((stockId: number, symbol: string, name: string) => {
  setDeepAnalysisTarget({ stockId, symbol, name })
}, [setDeepAnalysisTarget])


const { toast } = useToast()

const moveById = <T extends { id: number }>(list: T[], fromId: number, toId: number): T[] => {
  const fromIdx = list.findIndex(x => x.id === fromId)
  const toIdx = list.findIndex(x => x.id === toId)
  if (fromIdx < 0 || toIdx < 0 || fromIdx === toIdx) return list
  const next = [...list]
  const [moved] = next.splice(fromIdx, 1)
  next.splice(toIdx, 0, moved)
  return next
}

const persistWatchlistOrder = useCallback(async (ordered: Stock[]) => {
  const payload = ordered.map((s, idx) => ({ id: s.id, sort_order: idx + 1 }))
  await fetchAPI('/stocks/reorder', {
    method: 'PUT',
    body: JSON.stringify({ items: payload }),
  })
}, [])

const previewWatchlistReorder = useCallback((fromId: number, toId: number) => {
  if (fromId === toId) return
  setStocks(prev => {
    const ordered = [...prev].sort((a, b) => Number(a.sort_order || 0) - Number(b.sort_order || 0) || a.id - b.id)
    const moved = moveById(ordered, fromId, toId)
    return moved.map((s, idx) => ({ ...s, sort_order: idx + 1 }))
  })
}, [setStocks])

const commitWatchlistReorder = useCallback(async () => {
  const current = stocks
  if (!current || current.length === 0) return
  try {
    await persistWatchlistOrder(current)
  } catch (e) {
    if (watchDragSnapshotRef.current) setStocks(watchDragSnapshotRef.current)
    toast(e instanceof Error ? e.message : '保存关注排序失败', 'error')
  }
}, [persistWatchlistOrder, stocks, toast, setStocks, watchDragSnapshotRef])

const persistPositionOrder = useCallback(async (ordered: Position[]) => {
  const payload = ordered.map((p, idx) => ({ id: p.id, sort_order: idx + 1 }))
  await fetchAPI('/positions/reorder/batch', {
    method: 'PUT',
    body: JSON.stringify({ items: payload }),
  })
}, [])

const previewPositionReorder = useCallback((accountId: number, fromId: number, toId: number) => {
  if (fromId === toId) return
  setPortfolioRaw(prev => {
    if (!prev) return prev
    const accountsNext = prev.accounts.map(acc => {
      if (acc.id !== accountId) return acc
      const moved = moveById(acc.positions || [], fromId, toId).map((p, idx) => ({ ...p, sort_order: idx + 1 }))
      return { ...acc, positions: moved }
    })
    return { ...prev, accounts: accountsNext }
  })
}, [setPortfolioRaw])

const commitPositionReorder = useCallback(async (accountId: number) => {
  const acc = portfolioRaw?.accounts?.find(a => a.id === accountId)
  const ordered = acc?.positions || []
  if (!ordered.length) return
  try {
    await persistPositionOrder(ordered)
  } catch (e) {
    if (positionDragSnapshotRef.current) setPortfolioRaw(positionDragSnapshotRef.current)
    toast(e instanceof Error ? e.message : '保存持仓排序失败', 'error')
  }
}, [persistPositionOrder, portfolioRaw, toast, positionDragSnapshotRef, setPortfolioRaw])

const isSuppressCardClick = () => {
  try {
    const until = (window as any).__panwatch_suppress_card_click_until
    return typeof until === 'number' && Date.now() < until
  } catch {
    return false
  }
}
const searchTimer = useRef<ReturnType<typeof setTimeout>>()
const dropdownRef = useRef<HTMLDivElement>(null)

// 非核心数据后台加载（不阻塞 UI）
const loadConfigAsync = useCallback(async () => {
  try {
    const [agentData, servicesData, channelsData] = await Promise.all([
      fetchAPI<AgentConfig[]>('/agents'),
      fetchAPI<AIService[]>('/providers/services', { cacheMode: 'reload' }),
      fetchAPI<NotifyChannel[]>('/channels', { cacheMode: 'reload' }),
    ])
    setAgents(agentData)
    setServices(servicesData)
    setChannels(channelsData)
  } catch (e) {
    console.warn('加载配置数据失败:', e)
  }
}, [setAgents, setChannels, setServices])

const load = useCallback(async () => {
  setLoadError(null)
  try {
    // 核心数据（立即需要）
    const [stockData, accountData] = await Promise.all([
      fetchAPI<Stock[]>('/stocks', { cacheMode: 'reload' }),
      fetchAPI<Account[]>('/accounts', { cacheMode: 'reload' }),
    ])
    setStocks(stockData)
    setAccounts(accountData)
    // 默认展开所有账户
    setExpandedAccounts(new Set(accountData.map((a: Account) => a.id)))
  } catch (e) {
    console.error(e)
    setLoadError(e instanceof Error ? e.message : '加载失败')
  } finally {
    setLoading(false)  // 提前解除阻塞
  }

  // 非核心数据（后台加载，不阻塞 UI）
  loadConfigAsync()

  // 市场状态（非核心，失败不影响页面）
  try {
    const marketStatusData = await fetchAPI<MarketStatus[]>('/stocks/markets/status')
    setMarketStatus(marketStatusData)
  } catch (e) {
    console.warn('获取市场状态失败:', e)
  }
}, [loadConfigAsync, setAccounts, setExpandedAccounts, setLoadError, setLoading, setMarketStatus, setStocks])

// quotes 走 latest-ref: 保持 loadPortfolio 身份稳定, 合并时取最新行情
// (quotes 由 WS 每 5s 推送, 直接进 deps 会让挂载 effect 反复重拉; E3 2026-09-09)
const quotesRef = useRef(quotes)
quotesRef.current = quotes
const loadPortfolio = useCallback(async () => {
  setPortfolioLoading(true)
  try {
    // 核心数据：仅本地账户/持仓
    const portfolioData = await fetchAPI<PortfolioSummary>('/portfolio/summary?include_quotes=false')
    setPortfolioRaw(portfolioData)
    setPortfolio(mergePortfolioQuotes(portfolioData, quotesRef.current))

    // 市场状态（非核心，失败不影响页面）
    try {
      const marketStatusData = await fetchAPI<MarketStatus[]>('/stocks/markets/status')
      setMarketStatus(marketStatusData)
    } catch (e) {
      console.warn('获取市场状态失败:', e)
    }
  } catch (e) {
    console.error(e)
    setLoadError(e instanceof Error ? e.message : '加载失败')
  } finally {
    setPortfolioLoading(false)
  }
}, [setLoadError, setMarketStatus, setPortfolio, setPortfolioLoading, setPortfolioRaw])

const buildQuoteItems = useCallback((): QuoteRequestItem[] => {
  const items: QuoteRequestItem[] = []
  const seen = new Set<string>()

  for (const stock of stocks) {
    const key = `${stock.market}:${stock.symbol}`
    if (seen.has(key)) continue
    seen.add(key)
    items.push({ symbol: stock.symbol, market: stock.market })
  }

  for (const account of portfolioRaw?.accounts || []) {
    for (const pos of account.positions) {
      const key = `${pos.market}:${pos.symbol}`
      if (seen.has(key)) continue
      seen.add(key)
      items.push({ symbol: pos.symbol, market: pos.market })
    }
  }

  return items
}, [stocks, portfolioRaw])

// 2026-08-12: SSE 推送需要最新 items(闭包陷阱), 用 ref 同步
const buildQuoteItemsRef = useRef(buildQuoteItems)
buildQuoteItemsRef.current = buildQuoteItems

const refreshQuotes = useCallback(async () => {
  const items = buildQuoteItems()
  if (items.length === 0) return

  setQuotesLoading(true)
  try {
    const data = await fetchAPI<QuoteResponse[]>('/quotes/batch', {
      method: 'POST',
      body: JSON.stringify({ items }),
    })
    const map: Record<string, QuoteSnapshot> = {}
    for (const item of data) {
      map[`${item.market}:${item.symbol}`] = {
        current_price: item.current_price ?? null,
        change_pct: item.change_pct ?? null,
        quote_time: item.quote_time ?? null,
        quote_date: item.quote_date ?? null,
        daily_pnl_period: item.daily_pnl_period ?? 'unknown',
      }
    }
    setQuotes(map)
    setLastRefreshTime(new Date())
  } catch (e) {
    console.warn('刷新行情失败:', e)
  } finally {
    setQuotesLoading(false)
  }
}, [buildQuoteItems, setLastRefreshTime, setQuotes, setQuotesLoading])

useEffect(() => {
  if (!portfolioRaw) return
  setPortfolio(mergePortfolioQuotes(portfolioRaw, quotes))
}, [portfolioRaw, quotes, setPortfolio])

// 2026-08-12 行情推送: 后端每 5s 批量推持仓行情(腾讯批量接口)。
// KI-025(2026-09-09): 统一走 envelope 客户端 —— SWP 头带 token(不进 URL/日志)、
// 断线 last_seq 补发、指数退避、4401 不重连。⚠️ 曾用 EventSource/SSE, uvicorn 下
// StreamingResponse+while True 生成器挂起, 改 WebSocket 稳定。
// 注意: WS 只推"持仓"标的; 自选(未持仓)仍靠 refreshQuotes 轮询兜底, 故轮询不撤。
const onQuoteTicks = useCallback((data: QuoteTickMap) => {
  setQuotes(prev => {
    const next = { ...prev }
    // ⚠️ ref 存的是函数, 必须调用拿数组(2026-08-12 白屏根因: 迭代了函数本身)
    const items = buildQuoteItemsRef.current()
    for (const item of items) {
      const q = data[item.symbol]
      if (!q || typeof q.price !== 'number') continue
      next[`${item.market}:${item.symbol}`] = {
        current_price: q.price,
        change_pct: q.change_pct ?? null,
        quote_time: null,
        quote_date: null,
        daily_pnl_period: prev[`${item.market}:${item.symbol}`]?.daily_pnl_period ?? 'unknown',
      }
    }
    return next
  })
}, [setQuotes])

useQuoteStream(onQuoteTicks)

// 刷新 K 线摘要（并发受限的单个请求，避免批量接口慢）；并防止重入
const refreshKlines = useCallback(async () => {
  if (klineRefreshInFlight.current) return klineRefreshInFlight.current
  const run = (async () => {
    const items = buildQuoteItems()
    if (items.length === 0) return
    const limit = 5
    const map: Record<string, KlineSummary> = {}
    let idx = 0
    const worker = async () => {
      while (idx < items.length) {
        const i = idx++
        const it = items[i]
        try {
          // v0.4.8.1: summary 冷启动(主力意图逐笔翻页)可到 20-30s, 默认 20s 超时会导致
          // 首轮大面积 abort → 技术徽章全部回落"观望"; 显式放宽到 45s
          const res = await fetchAPI<{ symbol: string; market: string; summary: KlineSummary }>(`/klines/${encodeURIComponent(it.symbol)}/summary?market=${encodeURIComponent(it.market)}`, { cacheMode: 'reload', timeoutMs: 45000 })
          if (res && (res as any).summary) {
            map[`${it.market}:${it.symbol}`] = (res as any).summary as KlineSummary
          }
        } catch {
          // ignore single failure
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => worker()))
    // 增量合并：本轮单只失败时保留旧值，避免技术徽章闪断/消失
    setKlineSummaries(prev => ({ ...prev, ...map }))
  })()
  klineRefreshInFlight.current = run
  try { await run } finally { klineRefreshInFlight.current = null }
}, [buildQuoteItems, klineRefreshInFlight, setKlineSummaries])

// 从建议池加载建议（包含历史建议和多来源建议）
const loadPoolSuggestions = useCallback(async () => {
  setPoolSuggestionsLoading(true)
  try {
    const data = await fetchAPI<Record<string, PoolSuggestion>>('/suggestions?include_expired=true')
    setPoolSuggestions(data)
  } catch (e) {
    console.warn('加载建议池失败:', e)
  } finally {
    setPoolSuggestionsLoading(false)
  }
}, [setPoolSuggestions, setPoolSuggestionsLoading])

const loadPriceAlertSummaries = useCallback(async () => {
  try {
    const rows = await fetchAPI<PriceAlertRuleSummary[]>('/price-alerts')
    const map: Record<string, { total: number; enabled: number }> = {}
    for (const r of rows || []) {
      const key = `${String(r.market || 'CN').toUpperCase()}:${String(r.stock_symbol || '').toUpperCase()}`
      if (!map[key]) map[key] = { total: 0, enabled: 0 }
      map[key].total += 1
      if (r.enabled) map[key].enabled += 1
    }
    setPriceAlertSummaryMap(map)
  } catch (e) {
    console.warn('加载提醒摘要失败:', e)
  }
}, [setPriceAlertSummaryMap])

// Load news for specific stock or all watchlist
const loadNews = useCallback(async (stockName?: string) => {
  setNewsLoading(true)
  try {
    const params = new URLSearchParams({ hours: '168', limit: '50' })  // 7天
    if (stockName) {
      // 直接传递股票名称，比代码更稳定
      params.set('names', stockName)
    }
    const newsData = await fetchAPI<NewsItem[]>(`/news?${params}`)
    setNews(newsData)
  } catch (e) {
    console.error('加载新闻失败:', e)
  } finally {
    setNewsLoading(false)
  }
}, [setNews, setNewsLoading])

const openKlineDialog = useCallback((symbol: string, market: string, name?: string, hasPosition?: boolean) => {
  setKlineDialogSymbol(symbol)
  setKlineDialogMarket(market || 'CN')
  setKlineDialogName(name)
  setKlineDialogHasPosition(!!hasPosition)
  const m = market || 'CN'
  setKlineDialogInitialSummary(klineSummaries[`${m}:${symbol}`] || null)
  setKlineDialogOpen(true)
}, [klineSummaries, setKlineDialogHasPosition, setKlineDialogInitialSummary, setKlineDialogMarket, setKlineDialogName, setKlineDialogOpen, setKlineDialogSymbol])

const openMinuteDialog = useCallback((symbol: string, market: string, name?: string) => {
  setMinuteDialogSymbol(symbol)
  setMinuteDialogMarket(market || 'CN')
  setMinuteDialogName(name)
  setMinuteDialogOpen(true)
}, [setMinuteDialogMarket, setMinuteDialogName, setMinuteDialogOpen, setMinuteDialogSymbol])

// Open news dialog - pass stock name for more stable search
const openNewsDialog = useCallback((stockName?: string) => {
  setNewsDialogSymbol(stockName || '')  // 存储名称用于 UI 显示
  setNewsDialogOpen(true)
  loadNews(stockName)
}, [loadNews, setNewsDialogOpen, setNewsDialogSymbol])

const navigate = useNavigate()

const openStockDetail = useCallback((stockSymbol: string, _stockMarket: string, _stockName?: string, _hasPosition?: boolean) => {
  // 工作台③(2026-09-13): 模态降级 → 点击跳个股工作台 /stocks/:symbol
  if (stockSymbol) navigate(`/stocks/${encodeURIComponent(stockSymbol)}`)
}, [navigate])

// ========== PC 右键菜单 ==========

const openStockContextMenu = useCallback((e: React.MouseEvent, stock: StockContextTarget) => {
  e.preventDefault()
  e.stopPropagation()
  setStockCtxMenu({ x: e.clientX, y: e.clientY, stock })
}, [setStockCtxMenu])

const addToWatchlistFromMenu = useCallback(async (stock: StockContextTarget) => {
  const exists = stocks.some(s => s.market === stock.market && s.symbol === stock.symbol)
  if (exists) {
    toast('该股票已在自选中', 'success')
    return
  }
  try {
    await stocksApi.create({ symbol: stock.symbol, name: stock.name || stock.symbol, market: stock.market || 'CN' })
    toast('已加入自选', 'success')
    load()
  } catch (e) {
    toast(e instanceof Error ? e.message : '加入自选失败', 'error')
  }
}, [stocks, toast, load])

const viewDetailFromMenu = useCallback((stock: StockContextTarget) => {
  openStockDetail(stock.symbol, stock.market || 'CN', stock.name || stock.symbol, stock.hasPosition)
}, [openStockDetail])

const paperTradeFromMenu = useCallback(() => {
  // §4.3 补齐(2026-09-01): 模拟盘并入影子, 指向 /shadow?tab=paper
  navigate('/shadow?tab=paper')
}, [navigate])

// 设计稿 v2.1 §13.2: 持仓/自选列表右键 → 行情页 + source 上下文(顶部显示持仓上下文卡)
const openQuoteFromMenu = useCallback((stock: StockContextTarget) => {
  const source = stock.hasPosition ? 'holdings' : 'watchlist'
  navigate(`/forecast?type=stock&symbol=${encodeURIComponent(stock.symbol)}&tab=fund&source=${source}`)
}, [navigate])

const formatPreviewTime = (iso: string, tz?: string): string => {
  try {
    const d = parseServerTime(iso)
    if (isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', {
      timeZone: tz || undefined,
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  } catch {
    return iso
  }
}

const effectiveSchedule = (agent: AgentConfig, stockAgent?: StockAgentInfo | null): string => {
  const local = (stockAgent?.schedule || '').trim()
  if (local) return local
  return (agent.schedule || '').trim()
}

// Refresh quotes only (decoupled from portfolio and scans)
const handleRefresh = useCallback(async () => {
  await Promise.all([
    refreshQuotes(),
    loadPoolSuggestions(),
    refreshKlines(),
  ])
}, [refreshQuotes, loadPoolSuggestions, refreshKlines])

// stocks/portfolioRaw 变化 → 刷新行情与 K 线摘要(评分徽章)。放在 loader 声明之后
// (refreshKlines 声明于 831 行, deps 不能前向引用)。
useEffect(() => {
  if (stocks.length === 0 && (!portfolioRaw || portfolioRaw.accounts.length === 0)) return
  refreshQuotes()
  // 刷新 K 线摘要（用于常驻评分徽章）
  ;(async () => {
    try { await refreshKlines() } catch {}
  })()
}, [stocks, portfolioRaw, refreshQuotes, refreshKlines])

// 挂载一次性拉全量。refreshQuotes/refreshKlines 依赖 buildQuoteItems(←stocks),
// 身份随数据变化, 直接进 deps 会在每次 load 后无限重拉 → latest-ref 只跑最新闭包(E3 2026-09-09)。
const mountLoadRef = useRef<() => void>(() => {})
mountLoadRef.current = () => { load(); loadPortfolio(); loadPoolSuggestions(); loadPriceAlertSummaries(); refreshKlines() }
useEffect(() => { mountLoadRef.current() }, [])

// 仅关注列表场景（无持仓）也要在列表加载后预取 K 线摘要，保证技术指标徽章可见
const watchlistKlineInitDone = useRef(false)
const klineMissingRetryRef = useRef<Record<string, number>>({})
useEffect(() => {
  if (watchlistKlineInitDone.current) return
  if (!stocks || stocks.length === 0) return
  watchlistKlineInitDone.current = true
  refreshKlines()
}, [stocks, refreshKlines])

// 关注列表变更后，自动补齐缺失的 K 线摘要（避免未配置 agent 时没有技术指标徽章）
useEffect(() => {
  if (!stocks || stocks.length === 0) return
  const now = Date.now()
  const retryGapMs = 2 * 60 * 1000
  const missing = stocks.filter(s => {
    const key = `${s.market || 'CN'}:${s.symbol}`
    if (klineSummaries[key]) return false
    const lastTry = klineMissingRetryRef.current[key] || 0
    return (now - lastTry) > retryGapMs
  })
  if (missing.length === 0) return
  for (const s of missing) {
    const key = `${s.market || 'CN'}:${s.symbol}`
    klineMissingRetryRef.current[key] = now
  }
  refreshKlines()
}, [stocks, klineSummaries, refreshKlines])

// Agent 配置弹窗：预览未来触发时间（用于自检工作日/周末语义）
useEffect(() => {
  if (!agentDialogStock) return
  if (!agents || agents.length === 0) return

  const stockAgentMap = new Map((agentDialogStock.agents || []).map(a => [a.agent_name, a]))
  const schedules = new Set<string>()
  for (const agent of agents) {
    if (agent.execution_mode === 'batch') continue
    const sa = stockAgentMap.get(agent.name)
    if (!sa) continue
    const eff = effectiveSchedule(agent, sa)
    if (eff) schedules.add(eff)
  }

  const toFetch = Array.from(schedules).filter(s => !schedulePreviewCache[s] && !schedulePreviewLoading[s])
  if (toFetch.length === 0) return

  let cancelled = false
  ;(async () => {
    // Mark loading
    setSchedulePreviewLoading(prev => {
      const next = { ...prev }
      for (const s of toFetch) next[s] = true
      return next
    })
    try {
      const pairs = await Promise.all(toFetch.map(async s => {
        try {
          const p = await fetchAPI<SchedulePreview>(`/agents/schedule/preview?schedule=${encodeURIComponent(s)}&count=5`)
          return [s, p] as const
        } catch (e) {
          const msg = e instanceof Error ? e.message : '预览失败'
          return [s, { error: msg }] as const
        }
      }))
      if (cancelled) return
      setSchedulePreviewCache(prev => ({ ...prev, ...Object.fromEntries(pairs) }))
    } finally {
      if (cancelled) return
      setSchedulePreviewLoading(prev => {
        const next = { ...prev }
        for (const s of toFetch) next[s] = false
        return next
      })
    }
  })()

  return () => { cancelled = true }
}, [agentDialogStock, agents, schedulePreviewCache, schedulePreviewLoading, setSchedulePreviewCache, setSchedulePreviewLoading])

// 触发扫描：调用盘中监控扫描，并刷新建议池
// 2026-08-18 fix: analyze=false 默认(不调 LLM,5.9s 完成), 用户可手动点"AI 分析"
const scanAndReload = useCallback(async (analyze = false) => {
  setScanning(true)
  try {
    const url = `/agents/intraday/scan?analyze=${analyze}`
    await fetchAPI(url, { method: 'POST', timeoutMs: analyze ? 120000 : 30000 })
    await loadPoolSuggestions()
    await refreshKlines()
    setLastRefreshTime(new Date())
  } catch (e) {
    console.error('扫描失败:', e)
    toast(e instanceof Error ? e.message : '扫描失败', 'error')
  } finally {
    setScanning(false)
  }
}, [loadPoolSuggestions, refreshKlines, toast, setLastRefreshTime, setScanning])

// 首次加载后，按需刷新 K 线摘要与建议池
const initialKlineDone = useRef(false)
useEffect(() => {
  if (portfolio && portfolio.accounts.length > 0 && !initialKlineDone.current) {
    initialKlineDone.current = true
    refreshKlines()
    loadPoolSuggestions()
  }
}, [portfolio, refreshKlines, loadPoolSuggestions])

// Auto-refresh timer
useEffect(() => {
  if (autoRefresh) {
    refreshQuotes()
    refreshKlines()
    loadPoolSuggestions()
    refreshTimerRef.current = setInterval(() => {
      refreshQuotes()
      refreshKlines()
      loadPoolSuggestions()
    }, refreshInterval * 1000)
  } else {
    // Clear interval when disabled
    if (refreshTimerRef.current) {
      clearInterval(refreshTimerRef.current)
      refreshTimerRef.current = undefined
    }
  }

  return () => {
    if (refreshTimerRef.current) {
      clearInterval(refreshTimerRef.current)
    }
  }
}, [autoRefresh, refreshInterval, refreshQuotes, refreshKlines, loadPoolSuggestions, refreshTimerRef])


  return {
    openDeepAnalysis,
    previewWatchlistReorder,
    commitWatchlistReorder,
    previewPositionReorder,
    commitPositionReorder,
    isSuppressCardClick,
    searchTimer,
    dropdownRef,
    load,
    loadPortfolio,
    loadPriceAlertSummaries,
    loadNews,
    openKlineDialog,
    openMinuteDialog,
    openNewsDialog,
    openStockDetail,
    openStockContextMenu,
    addToWatchlistFromMenu,
    viewDetailFromMenu,
    paperTradeFromMenu,
    openQuoteFromMenu,
    formatPreviewTime,
    effectiveSchedule,
    handleRefresh,
    scanAndReload,
  }
}
