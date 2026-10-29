import { useEffect, useRef, useCallback, useState } from 'react'
import type { AIService } from '@sida/api'
import type { Account } from './shared'
import type { AgentConfig } from './shared'
import type { KlineSummary } from '@sida/biz-ui/components/suggestion-badge'
import type { MarketStatus } from './shared'
import type { NewsItem } from './shared'
import type { NotifyChannel } from '@sida/api'
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
import { fetchAPI, normalizeNewsEnvelope } from '@sida/api'
import { useQuoteStream, type QuoteTickMap } from '@/realtime/useQuoteStream'
import { mergePortfolioQuotes } from './shared'
import { applyQuoteTicks, expireQuoteSnapshots } from './quote-ticks'
import { parseServerTime } from '@/lib/utils'
import { stocksApi } from '@sida/api'
import { useNavigate } from 'react-router-dom'
import { useToast } from '@sida/base-ui/components/ui/toast'
import { usePausedInterval } from '@sida/base-ui'
import type { useStocksState } from './useStocksState'
import { logger } from '@/lib/logger'
import { navBackState } from '@/lib/nav-back'

/**
 * 行情时效心跳（毫秒）—— **与用户的「自动刷新」开关无关**（2026-10-29 独立复审 F1）。
 *
 * 旧实现把 `/quotes/batch` 与 `/stocks/markets/status` 的周期刷新只挂在
 * `usePausedInterval(autoRefreshTick, autoRefresh ? refreshInterval*1000 : 0)` 上，
 * 而 `autoRefresh` 默认 **false**（`sida_stocks_autoRefresh`）⇒ 默认配置下两边一次都不再刷新：
 * 09:31 拉到的报价和 09:31 的服务器市场墙钟**一起冻结**，"报价距市场时钟多久"永远算出
 * 0 分钟 ⇒ 14:00 仍把上午的价判成 `current`。关掉自动刷新只该意味着"别替我扫盘/别刷 K 线"，
 * 不该意味着"允许行情与时点证明冻结"。
 *
 * 取值 20s：时效窗口 `QUOTE_FRESH_WINDOW_MINUTES = 15` 分钟是**推断值**（不是交易所口径），
 * 20s 远小于它，够快；又不至于像 WS 那样每 5s 打一次批量接口（隐藏页签还会停表）。
 */
export const FRESHNESS_HEARTBEAT_MS = 20_000

/**
 * 服务器市场墙钟的**本地有效期**（毫秒）—— 2026-10-29 复审 B1。
 *
 * 心跳每 `FRESHNESS_HEARTBEAT_MS` 拉一次 `/stocks/markets/status`。可是"请求发出去了"
 * 不等于"时钟被刷新了"：网关先回响应头、响应体挂住时（旧版 `fetchAPI` 的超时只覆盖到
 * 响应头），这个 promise 永不 settle，`setMarketStatus` 也永不执行 ——
 * 页面就带着 09:31 的墙钟一直跑到 14:00，把 09:31 的报价判成 `current`。
 *
 * 所以墙钟另有一条**不看请求状态**的本地有效期：最后一次成功取到之后超过这么久，
 * 就直接清空时钟（下游判 `no_clock` = 时点未证实）。60s = 3 个心跳，容忍偶发丢包，
 * 但绝不允许"几个小时前的钟"继续给报价背书。
 */
export const MARKET_CLOCK_TTL_MS = 60_000

/**
 * 行情快照的本地有效期（毫秒）—— 2026-10-29 复审 B1 / B3。
 *
 * `quote_time` / `quote_date` / `daily_pnl_period` 只由批量轮询刷新；WS tick 只更价。
 * 轮询长时间失败时，快照会"看起来刚被写过"（价格一直在跳动），日期却可能是昨天的。
 * 超过这个窗口没成功轮询过 ⇒ 清掉时刻/日期/期间与涨跌幅，只留价格陈列：
 * 下游判 `unknown`，当日盈亏也不把它算进篮子。
 *
 * 180s 的理由：休市时批量行情按 `IDLE_QUOTE_REFRESH_MS`（120s）降频，180s 留一次容错；
 * 交易中 20s 一轮，三分钟没成功基本就是链路坏了。
 */
export const QUOTE_SNAPSHOT_TTL_MS = 180_000

/**
 * 休市时批量行情的刷新间隔（毫秒）—— 2026-10-29 复审「20s 全自选流量」。
 *
 * 交易中需要 20s 级时效；收盘后价格不再变化，再按 20s 把**每个在线用户的全自选**
 * 打一遍批量接口只是纯流量（行情源上游按分钟级限流，多用户并发下最容易打 429）。
 * 休市降到 120s；市场墙钟仍按 20s 刷（那是后端纯本地计算，不触上游）。
 *
 * 诚实声明：本仓库没有压测环境，**没有验证过**这个降频 + 在途合并 + `fetchAPI` 同键
 * 去重在多用户下的实际 QPS；这里只能保证"每用户每秒的请求数量级降下来了"。
 */
export const IDLE_QUOTE_REFRESH_MS = 120_000

/** 从 `/stocks/markets/status` 折出 {市场: 当地时分}（`HH:MM`）；解析不出的市场不出现。 */
function clockMinutesOfRows(rows: readonly MarketStatus[] | null | undefined): Record<string, number> {
  const out: Record<string, number> = {}
  for (const row of Array.isArray(rows) ? rows : []) {
    const code = String(row?.code ?? '').trim().toUpperCase()
    if (!code) continue
    const m = /^(\d{1,2}):(\d{2})/.exec(String(row?.local_time ?? '').trim())
    if (!m) continue
    const h = Number(m[1])
    const min = Number(m[2])
    if (!Number.isFinite(h) || !Number.isFinite(min) || h > 23 || min > 59) continue
    out[code] = h * 60 + min
  }
  return out
}

/** 上一个成功采样的市场墙钟 → 这次采样，是否有市场**回绕**（跨日）。 */
function hasMarketDayRollover(prev: Record<string, number>, next: Record<string, number>): boolean {
  for (const [market, minutes] of Object.entries(next)) {
    const before = prev[market]
    // 只认"明显倒退"（>5 分钟）：15:03 → 09:31 是第二天开盘；
    // 几秒级抖动/时钟微调不算跨日，也不该清报价。
    if (before !== undefined && minutes < before - 5) return true
  }
  return false
}

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
    marketStatus,
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

/**
 * 行情时效的两条**本地**心跳记账（2026-10-29 复审 B1）。
 *
 * 它们不表示"请求发出去了"，只表示"**成功拿到过**"，所以即使请求挂死/被同键合并成
 * 一条永不 settle 的 promise，本地也能按有效期把旧墙钟/旧报价降级成 unknown。
 *  - `marketClockSampledAtRef`：最近一次成功取到服务器市场墙钟的时刻；
 *  - `lastMarketClockRef`：上一次采样的各市场当地分钟数（用来识别跨日回绕）；
 *  - `quotePolledAtRef`：最近一次批量行情成功的时刻（快照日期层的有效期起点）。
 */
const marketClockSampledAtRef = useRef(0)
const lastMarketClockRef = useRef<Record<string, number>>({})
const quotePolledAtRef = useRef(0)

/**
 * 「手里这份报价凭证是否仍被成功续期」（2026-10-29 F2/B1 的渲染侧开关）。
 *
 * 上面三个 ref 只会计账，不会触发重渲染；而持仓风险清单必须**看到**降级结果才会把
 * 结论从「破位」改成「时效未核实」。所以另存一个 state：
 *   - 批量行情成功一次 ⇒ true；
 *   - 批量行情失败 / 快照超过 `QUOTE_SNAPSHOT_TTL_MS` 被清 ⇒ false。
 * 初值 true：还没发过请求时由持仓数据本身的字段决定，不该无端把所有行标成未核实。
 */
const [quoteEvidenceVerified, setQuoteEvidenceVerified] = useState(true)

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
    const until = (window as any).__sida_suppress_card_click_until
    return typeof until === 'number' && Date.now() < until
  } catch {
    return false
  }
}
const searchTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
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
    logger.warn('加载配置数据失败:', e)
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
    logger.error(e)
    const msg = e instanceof Error ? e.message : '加载失败'
    if (msg.includes('429')) {
      logger.warn('页面基础数据刷新触发限流退避，沿用当前状态')
    } else {
      setLoadError(msg)
    }
  } finally {
    setLoading(false)  // 提前解除阻塞
  }

  // 非核心数据（后台加载，不阻塞 UI）
  loadConfigAsync()

  // 市场状态（非核心，失败不影响页面）
  try {
    // 2026-10-29: 必须 `reload` —— `fetchAPI` 对 GET 有 30s 内存缓存，命中缓存会让
    // "市场日期/墙钟"停在缓存那一刻（跨日时旧日期继续给新报价背书）。
    const marketStatusData = await fetchAPI<MarketStatus[]>('/stocks/markets/status', { cacheMode: 'reload' })
    setMarketStatus(marketStatusData)
    // 成功拿到才算一次有效采样（本地有效期起点，见 marketClockSampledAtRef）
    lastMarketClockRef.current = clockMinutesOfRows(marketStatusData)
    marketClockSampledAtRef.current = Date.now()
  } catch (e) {
    logger.warn('获取市场状态失败:', e)
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
      const marketStatusData = await fetchAPI<MarketStatus[]>('/stocks/markets/status', { cacheMode: 'reload' })
      setMarketStatus(marketStatusData)
      lastMarketClockRef.current = clockMinutesOfRows(marketStatusData)
      marketClockSampledAtRef.current = Date.now()
    } catch (e) {
      logger.warn('获取市场状态失败:', e)
    }
    setLoadError(null)
  } catch (e) {
    logger.error(e)
    const msg = e instanceof Error ? e.message : '加载失败'
    // 若仅是 429 限流或网络暂态抖动且已有持仓数据，沿用既有快照，不弹红框
    if (msg.includes('429') && portfolioRaw) {
      logger.warn('持仓刷新触发限流退避，保留当前数据')
    } else {
      setLoadError(msg)
    }
  } finally {
    setPortfolioLoading(false)
  }
}, [portfolioRaw, setLoadError, setMarketStatus, setPortfolio, setPortfolioLoading, setPortfolioRaw])

/**
 * 刷新服务器市场状态（2026-10-29 三次复审）。
 *
 * 持仓风险检查/防线告警的「行情时刻层」用 `/stocks/markets/status` 的
 * `local_time`（后端按市场时区算的服务器墙钟）+ `status`（后端按交易日历判的时段）。
 * 这两个值必须跟着轮询一起刷新：页面开了一上午而时钟停在 09:00 时，
 * 「报价 09:31 距今多久」就无从判断 —— 只能退化成「时点未知」，
 * 于是用户在整段时间里都看不到真实结论。端点本身是纯本地计算（无外部行情请求）。
 *
 * 2026-10-29 独立复审 F1：失败时**把墙钟清空**，不保留上一次的时钟。
 * 一个变老的时钟比没有时钟更糟 —— 沿用旧墙钟会让"报价距今多久"恒为 0（冒充实时），
 * 或与已刷新过的新报价算成负年龄。清空后下游判成 `no_clock`（时点未证实），是保守的解。
 * 并发用 in-flight 闸门挡住：上一轮没回就不叠第二轮（心跳与 autoRefresh 可能同时到点）。
 */
const marketStatusInFlightRef = useRef<Promise<void> | null>(null)
const refreshMarketStatus = useCallback(async () => {
  if (marketStatusInFlightRef.current) return marketStatusInFlightRef.current
  const run = (async () => {
    try {
      const rows = await fetchAPI<MarketStatus[]>('/stocks/markets/status', { cacheMode: 'reload' })
      const nextClock = clockMinutesOfRows(rows)
      // 2026-10-29 复审 B3：服务器市场墙钟**回绕**（15:03 → 09:31）就是"跨日"的可核对信号。
      // 这时上一轮快照的 `quote_date` / `daily_pnl_period` 属于昨天，必须立刻作废 ——
      // 否则第二天同一分钟（09:31 对 09:31）会被算成"距今 0 分钟"。
      // 只认明显倒退（>5 分钟），不把时钟微调当跨日。
      if (hasMarketDayRollover(lastMarketClockRef.current, nextClock)) {
        quotePolledAtRef.current = 0
        setQuotes(prev => expireQuoteSnapshots(prev))
      }
      lastMarketClockRef.current = nextClock
      marketClockSampledAtRef.current = Date.now()
      setMarketStatus(rows)
    } catch (e) {
      logger.warn('刷新市场状态失败:', e)
      // 没有可信的服务器墙钟 ⇒ 时效判定必须退化成"时点未证实"（见上）。
      marketClockSampledAtRef.current = 0
      lastMarketClockRef.current = {}
      setMarketStatus([])
    }
  })()
  marketStatusInFlightRef.current = run
  try {
    await run
  } finally {
    marketStatusInFlightRef.current = null
  }
}, [setMarketStatus, setQuotes])

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

const quotesInFlightRef = useRef<Promise<void> | null>(null)
/** 在飞那一轮请求覆盖的标的集合；用来判断"数据变了没有"（变了才需要补一轮）。 */
const quotesInFlightSigRef = useRef<string>('')
/** 在飞期间标的集合变过 ⇒ 结束当前轮后立刻按新集合再跑一轮（不并发、也不丢更新）。 */
const quotesPendingRef = useRef(false)
const quoteItemsSig = (items: readonly QuoteRequestItem[]) =>
  items.map(it => `${it.market}:${it.symbol}`).sort().join(',')

const refreshQuotes = useCallback(async () => {
  const items = buildQuoteItems()
  if (items.length === 0) return

  // 并发闸门（2026-10-29 独立复审 F1）：时效心跳、autoRefresh、数据变更 effect
  // 现在都可能在同一时刻要求刷新（3 条路径）。上一轮没回就**复用**它，不再叠一轮批量行情
  // —— 叠起来只会把行情源打成 429，反而让时效更差。
  // 例外：若这期间**标的集合变了**（新增持仓/自选），记一笔 pending，等当前轮结束立刻补一轮
  // （否则新标的要等到下个心跳才有价，等于让"加自选"慢半分钟生效）。
  if (quotesInFlightRef.current) {
    if (quoteItemsSig(items) !== quotesInFlightSigRef.current) quotesPendingRef.current = true
    return quotesInFlightRef.current
  }
  quotesInFlightSigRef.current = quoteItemsSig(items)
  const run = (async () => {
    let first = true
    do {
      quotesPendingRef.current = false
      // 补跑必须读**最新**的标的集合（ref 每次渲染更新）；闭包里的 buildQuoteItems
      // 还是发起那一轮的快照，拿它补跑等于又打一遍旧集合。
      const batch = first ? items : buildQuoteItemsRef.current()
      first = false
      if (batch.length === 0) return
      quotesInFlightSigRef.current = quoteItemsSig(batch)
      setQuotesLoading(true)
      try {
        const data = await fetchAPI<QuoteResponse[]>('/quotes/batch', {
          method: 'POST',
          body: JSON.stringify({ items: batch }),
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
        // 快照日期层的有效期起点：只有**成功轮询**才刷新它（WS tick 不算）。
        quotePolledAtRef.current = Date.now()
        setQuoteEvidenceVerified(true)
        setLastRefreshTime(new Date())
      } catch (e) {
        logger.warn('刷新行情失败:', e)
        // 2026-10-29 F2: 续期失败 ⇒ 手里这份凭证已无从证明是当下的，清单要能看见。
        setQuoteEvidenceVerified(false)
      } finally {
        setQuotesLoading(false)
      }
    } while (quotesPendingRef.current)
  })()
  quotesInFlightRef.current = run
  try {
    await run
  } finally {
    quotesInFlightRef.current = null
  }
}, [buildQuoteItems, setLastRefreshTime, setQuotes, setQuotesLoading, setQuoteEvidenceVerified])

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
  // ⚠️ ref 存的是函数, 必须调用拿数组(2026-08-12 白屏根因: 迭代了函数本身)
  // 合并规则在 `./quote-ticks`（纯函数，有测试）：帧里带可靠 quote_time 才更新时效，
  // 否则只更价、时刻明确判未知 —— 绝不沿用上一轮旧时刻给新价格背书，也不用本机时钟造时刻。
  // 2026-10-29 独立复审 F3（上一版"保留旧时刻"的写法正是复审阻断：14:00 的价配 09:31 的时刻）。
  setQuotes(prev => applyQuoteTicks(prev, buildQuoteItemsRef.current(), data))
}, [setQuotes])

useQuoteStream(onQuoteTicks)

// 刷新 K 线摘要（并发受限的单个请求，避免批量接口慢）；并防止重入
const refreshKlines = useCallback(async (only?: Array<{ symbol: string; market?: string }>) => {
  if (klineRefreshInFlight.current) return klineRefreshInFlight.current
  const run = (async () => {
    // 2026-10-21: 允许只刷指定的几只。原来"补齐缺失"时会全量刷新，只要有一只票持续失败，
    // 就会每轮把所有标的重新打一遍（线上把单只端点打到 1264 次/分钟，触发 429 图表空白）。
    const items = only && only.length ? only : buildQuoteItems()
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
          const res = await fetchAPI<{ symbol: string; market: string; summary: KlineSummary }>(`/klines/${encodeURIComponent(it.symbol)}/summary?market=${encodeURIComponent(it.market || 'CN')}`, { cacheMode: 'reload', timeoutMs: 45000 })
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
    logger.warn('加载建议池失败:', e)
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
    logger.warn('加载提醒摘要失败:', e)
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
    const newsData = await fetchAPI<unknown>(`/news?${params}`)
    const env = normalizeNewsEnvelope<NewsItem>(newsData)
    setNews(env.items)
  } catch (e) {
    logger.error('加载新闻失败:', e)
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
  if (stockSymbol) navigate(`/stocks/${encodeURIComponent(stockSymbol)}`, { state: navBackState('/portfolio', '持仓') })
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
  // 只补缺失的那几只（不再全量重刷），并限制同时最多 8 只，避免又变成风暴
  void refreshKlines(missing.slice(0, 8))
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
    logger.error('扫描失败:', e)
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
// 页签 keep-alive(2026-10-15): 轮询走 `usePausedInterval` ⇒ **隐藏页签停表**(不发请求), 回前台补一次;
// 用户关掉自动刷新 ⇒ `delayMs<=0` 停用(原 `else` 分支的"清表"由此承担)。
const autoRefreshTick = useCallback(() => {
  refreshQuotes()
  refreshKlines()
  loadPoolSuggestions()
  // 服务器市场墙钟必须与行情同频，否则「报价距今多久」会随页面变老而失真。
  void refreshMarketStatus()
}, [refreshQuotes, refreshKlines, loadPoolSuggestions, refreshMarketStatus])
usePausedInterval(autoRefreshTick, autoRefresh ? refreshInterval * 1000 : 0, {
  deps: [autoRefresh, refreshInterval],
})

/**
 * 本地有效期检查（2026-10-29 复审 B1）—— **不看请求状态**，只看"最后一次成功取到"。
 *
 * 为什么必要：`fetchAPI` 的超时以前只覆盖到响应头（已修，见 `packages/api`），但即使
 * 修好超时，链路也可能连续几轮都失败。此时请求状态是不可信的，唯一可靠的是本地记账：
 *  - 墙钟超期 ⇒ 清空市场状态（下游判 `no_clock`，报价一律"时点未证实"）；
 *  - 报价快照超期 ⇒ 清掉时刻/日期/期间与涨跌幅，只留价格陈列（下游判 `unknown`，
 *    当日盈亏也不把它算进"今日"篮子）。
 * 两者都独立于 autoRefresh 开关与 WS 是否活着。
 */
const expireStaleSnapshots = useCallback(() => {
  const now = Date.now()
  if (marketClockSampledAtRef.current > 0 && now - marketClockSampledAtRef.current > MARKET_CLOCK_TTL_MS) {
    marketClockSampledAtRef.current = 0
    lastMarketClockRef.current = {}
    setMarketStatus([])
  }
  if (quotePolledAtRef.current > 0 && now - quotePolledAtRef.current > QUOTE_SNAPSHOT_TTL_MS) {
    quotePolledAtRef.current = 0
    setQuoteEvidenceVerified(false)
    const hasEvidence = Object.values(quotesRef.current).some(
      q => q.quote_time !== null || q.quote_date !== null || q.daily_pnl_period !== 'unknown' || q.change_pct !== null,
    )
    if (hasEvidence) setQuotes(prev => expireQuoteSnapshots(prev))
  }
}, [setMarketStatus, setQuotes, setQuoteEvidenceVerified])

/**
 * 现在该不该轮询批量行情（2026-10-29 复审「20s 全自选流量」）。
 *
 * - 有市场在交易（或状态未知）⇒ 按心跳节奏（20s）；
 * - **已知**所有相关市场都休市 ⇒ 降到 `IDLE_QUOTE_REFRESH_MS`（120s）；
 *   收盘后价格不动，20s 一轮只是把每个在线用户的全自选白白打一遍。
 * 市场墙钟不受此限制（后端纯本地计算，按 20s 刷）。
 *
 * ⚠️ 必须读 **ref** 而不是闭包里的 `marketStatus`：`usePausedInterval` 在回调身份变化时
 * 会重新挂表并且 `immediate:true` 立刻再跑一次。若把 `marketStatus` 放进 `useCallback`
 * 依赖，每次写回市场状态（新数组）都会改变回调身份 ⇒ 立刻再请求 ⇒ 再写回 ⇒ 自激请求环。
 * 这里固定身份（`[]` 依赖），状态走 `marketStatusRef`（每次渲染同步）。
 */
const marketStatusRef = useRef<MarketStatus[]>([])
marketStatusRef.current = Array.isArray(marketStatus) ? marketStatus : []
const shouldPollQuotesNow = useCallback((now: number): boolean => {
  const rows = marketStatusRef.current
  const tracked = new Set(
    buildQuoteItemsRef.current().map(it => String(it.market ?? 'CN').trim().toUpperCase()),
  )
  const relevant = rows.filter(r => tracked.has(String(r?.code ?? '').trim().toUpperCase()))
  const scope = relevant.length > 0 ? relevant : rows
  const anyTrading =
    scope.length === 0 || scope.some(r => String(r?.status ?? '').trim().toLowerCase() === 'trading')
  const gap = anyTrading ? FRESHNESS_HEARTBEAT_MS : IDLE_QUOTE_REFRESH_MS
  return now - quotePolledAtRef.current >= gap
}, [])

/**
 * 行情时效心跳（2026-10-29 独立复审 F1）—— **不读 autoRefresh**。
 *
 * 只做三件事：清掉过期快照（本地有效期，B1）+ 刷新服务器市场墙钟 + 按需刷新本地行情。
 * 三者是"这条报价是不是当下的"这个判断的**唯一外部依据**（`freshnessFor` 的输入），
 * 一旦随用户开关一起冻住，持仓风险与防线告警就会拿上午的价说"现在破位"。
 *
 * 刻意不在这里刷 K 线/建议池/账户汇总：那些是"用户主动要的更重的东西"，
 * 关掉自动刷新时不该被偷偷拉起（性能与配额都受不起）。
 * 页签隐藏 ⇒ `usePausedInterval` 停表；回前台 ⇒ 立即补一次；卸载 ⇒ 清定时器。
 * 并发由 `refreshQuotes` / `refreshMarketStatus` 各自的 in-flight 闸门兜住。
 */
const freshnessHeartbeat = useCallback(() => {
  expireStaleSnapshots()
  void refreshMarketStatus()
  if (shouldPollQuotesNow(Date.now())) void refreshQuotes()
}, [expireStaleSnapshots, refreshMarketStatus, refreshQuotes, shouldPollQuotesNow])
usePausedInterval(freshnessHeartbeat, FRESHNESS_HEARTBEAT_MS, {
  deps: [freshnessHeartbeat],
})


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
    /** 报价凭证是否仍被成功续期（`PositionRiskSection` 据此降级「时效未核实」）。 */
    quoteEvidenceVerified,
  }
}
