import { useState, useRef } from 'react'
import type { AIService } from '@panwatch/api'
import type { Account } from './shared'
import type { AccountForm } from './shared'
import type { AgentConfig } from './shared'
import type { KlineSummary } from '@panwatch/biz-ui/components/suggestion-badge'
import type { MarketStatus } from './shared'
import type { NewsItem } from './shared'
import type { NotifyChannel } from '@panwatch/api'
import type { PoolSuggestion } from './shared'
import type { PortfolioSummary } from './shared'
import type { PositionForm } from './shared'
import type { QuoteSnapshot } from './shared'
import type { SchedulePreview } from './shared'
import type { SearchResult } from './shared'
import type { Stock } from './shared'
import type { StockContextMenuState } from '@/components/StockContextMenu'
import type { StockForm } from './shared'
import type { StockSuggestionData } from './shared'
import { emptyAccountForm } from './shared'
import { emptyStockForm } from './shared'
import { useLocalStorage } from '@/lib/utils'

export type ViewTab = 'positions' | 'watchlist'

export function useStocksState() {
const [stocks, setStocks] = useState<Stock[]>([])
const [accounts, setAccounts] = useState<Account[]>([])
const [agents, setAgents] = useState<AgentConfig[]>([])
const [services, setServices] = useState<AIService[]>([])
const [channels, setChannels] = useState<NotifyChannel[]>([])
const [loading, setLoading] = useState(true)
// 初始加载失败提示(失败≠空态:避免把"加载失败"误读为"没有数据")
const [loadError, setLoadError] = useState<string | null>(null)

// Portfolio
const [portfolio, setPortfolio] = useState<PortfolioSummary | null>(null)
const [portfolioRaw, setPortfolioRaw] = useState<PortfolioSummary | null>(null)
const [portfolioLoading, setPortfolioLoading] = useState(false)
const [expandedAccounts, setExpandedAccounts] = useState<Set<number>>(new Set())

// Quotes for all stocks (used in stock list)
const [quotes, setQuotes] = useState<Record<string, QuoteSnapshot>>({})
const [quotesLoading, setQuotesLoading] = useState(false)
// Keyed by `${market}:${symbol}` to avoid cross-market symbol collisions
const [klineSummaries, setKlineSummaries] = useState<Record<string, KlineSummary>>({})

// Auto-refresh (持久化到 localStorage)
const [autoRefresh, setAutoRefresh] = useLocalStorage('panwatch_stocks_autoRefresh', false)
const [refreshInterval, setRefreshInterval] = useLocalStorage('panwatch_stocks_refreshInterval', 30)
const [lastRefreshTime, setLastRefreshTime] = useState<Date | null>(null)
const refreshTimerRef = useRef<ReturnType<typeof setInterval>>()

// Alerts / Scanning
const [scanning, setScanning] = useState(false)

type ViewTab = 'positions' | 'watchlist'
const [viewTab, setViewTab] = useLocalStorage<ViewTab>('panwatch_stocks_viewTab', 'positions')

// 股票 AI 建议（来自盘中监控 API）
const [suggestions] = useState<Record<string, StockSuggestionData>>({})
// 建议池建议（来自 /suggestions API）
const [poolSuggestions, setPoolSuggestions] = useState<Record<string, PoolSuggestion>>({})
const [poolSuggestionsLoading, setPoolSuggestionsLoading] = useState(false)
const [priceAlertSummaryMap, setPriceAlertSummaryMap] = useState<Record<string, { total: number; enabled: number }>>({})

// News Dialog
const [newsDialogOpen, setNewsDialogOpen] = useState(false)
const [newsDialogSymbol, setNewsDialogSymbol] = useState<string>('')  // 空=全部, 否则=指定股票
const [news, setNews] = useState<NewsItem[]>([])
const [newsLoading, setNewsLoading] = useState(false)

// Kline Dialog
const [klineDialogOpen, setKlineDialogOpen] = useState(false)
const [klineDialogSymbol, setKlineDialogSymbol] = useState('')
const [klineDialogMarket, setKlineDialogMarket] = useState('CN')
const [klineDialogName, setKlineDialogName] = useState<string | undefined>(undefined)
const [klineDialogHasPosition, setKlineDialogHasPosition] = useState<boolean>(false)
const [klineDialogInitialSummary, setKlineDialogInitialSummary] = useState<KlineSummary | null>(null)
// Minute Dialog (分时候时)
const [minuteDialogOpen, setMinuteDialogOpen] = useState(false)
const [minuteDialogSymbol, setMinuteDialogSymbol] = useState('')
const [minuteDialogMarket, setMinuteDialogMarket] = useState('CN')
const [minuteDialogName, setMinuteDialogName] = useState<string | undefined>(undefined)
const [insightOpen, setInsightOpen] = useState(false)
const [insightSymbol, setInsightSymbol] = useState('')
const [insightMarket, setInsightMarket] = useState('CN')
const [insightName, setInsightName] = useState<string | undefined>(undefined)
const [insightHasPosition, setInsightHasPosition] = useState(false)

// PC 右键菜单(股票行)
const [stockCtxMenu, setStockCtxMenu] = useState<StockContextMenuState | null>(null)

// Market status
const [marketStatus, setMarketStatus] = useState<MarketStatus[]>([])
// Guard to prevent overlapping K线刷新任务导致实际并发超限
const klineRefreshInFlight = useRef<Promise<void> | null>(null)

// Stock form
const [showStockForm, setShowStockForm] = useState(false)
const [stockForm, setStockForm] = useState<StockForm>(emptyStockForm)
const [searchQuery, setSearchQuery] = useState('')
const [searchMarket, setSearchMarket] = useState('')  // 搜索市场筛选
const [searchResults, setSearchResults] = useState<SearchResult[]>([])
const [showDropdown, setShowDropdown] = useState(false)
const [searching, setSearching] = useState(false)
const [refreshingStockList, setRefreshingStockList] = useState(false)

// Account form
const [accountDialogOpen, setAccountDialogOpen] = useState(false)
const [accountForm, setAccountForm] = useState<AccountForm>(emptyAccountForm)
const [editAccountId, setEditAccountId] = useState<number | null>(null)

// Position form
const [positionDialogOpen, setPositionDialogOpen] = useState(false)
const [positionForm, setPositionForm] = useState<PositionForm>({ account_id: 0, stock_id: 0, cost_price: '', quantity: '', invested_amount: '', trading_style: '', stock_symbol: '', stock_name: '', stock_market: 'CN' })
const [editPositionId, setEditPositionId] = useState<number | null>(null)
const [positionDialogAccountId, setPositionDialogAccountId] = useState<number | null>(null)
const [positionSearchQuery, setPositionSearchQuery] = useState('')
const [positionSearchMarket, setPositionSearchMarket] = useState('')  // 搜索市场筛选
const [positionSearchResults, setPositionSearchResults] = useState<SearchResult[]>([])
const [positionSearching, setPositionSearching] = useState(false)
const [showPositionDropdown, setShowPositionDropdown] = useState(false)
const positionSearchTimer = useRef<ReturnType<typeof setTimeout>>()
const positionDropdownRef = useRef<HTMLDivElement>(null)

// Agent dialog
const [agentDialogStock, setAgentDialogStock] = useState<Stock | null>(null)

// 深度分析(TradingAgents)弹窗
const [deepAnalysisTarget, setDeepAnalysisTarget] = useState<{
  stockId: number
  symbol: string
  name: string
} | null>(null)

const [triggeringAgent, setTriggeringAgent] = useState<string | null>(null)
const [schedulePreviewCache, setSchedulePreviewCache] = useState<Record<string, SchedulePreview | { error: string }>>({})
const [schedulePreviewLoading, setSchedulePreviewLoading] = useState<Record<string, boolean>>({})
// 运行中的单只股票 Agent（按股票标记具体 Agent 名称）
const [runningAgents, setRunningAgents] = useState<Record<number, string | null>>({})
const [agentResultDialog, setAgentResultDialog] = useState<{ title: string; content: string; should_alert: boolean; notified: boolean } | null>(null)

// Stock list filter
const [stockListFilter, setStockListFilter] = useState('')  // '' = 全部, 'CN' = A股, 'HK' = 港股, 'US' = 美股
const [watchlistOnlyAlerts, setWatchlistOnlyAlerts] = useLocalStorage<boolean>('panwatch_watchlist_only_alerts', false)

// Remove watchlist modal
const [removeWatchStock, setRemoveWatchStock] = useState<Stock | null>(null)
const [removingWatchStock, setRemovingWatchStock] = useState(false)
const [draggingWatchStockId, setDraggingWatchStockId] = useState<number | null>(null)
const [draggingPositionId, setDraggingPositionId] = useState<number | null>(null)
const [draggingPositionAccountId, setDraggingPositionAccountId] = useState<number | null>(null)
const watchDragSnapshotRef = useRef<Stock[] | null>(null)
const positionDragSnapshotRef = useRef<PortfolioSummary | null>(null)

  return {
    loading,
    stocks,
    setStocks,
    accounts,
    setAccounts,
    agents,
    setAgents,
    services,
    setServices,
    channels,
    setChannels,
    setLoading,
    loadError,
    setLoadError,
    portfolio,
    setPortfolio,
    portfolioRaw,
    setPortfolioRaw,
    portfolioLoading,
    setPortfolioLoading,
    expandedAccounts,
    setExpandedAccounts,
    quotes,
    setQuotes,
    quotesLoading,
    setQuotesLoading,
    klineSummaries,
    setKlineSummaries,
    autoRefresh,
    setAutoRefresh,
    refreshInterval,
    setRefreshInterval,
    lastRefreshTime,
    setLastRefreshTime,
    refreshTimerRef,
    scanning,
    setScanning,
    viewTab,
    setViewTab,
    suggestions,
    poolSuggestions,
    setPoolSuggestions,
    poolSuggestionsLoading,
    setPoolSuggestionsLoading,
    priceAlertSummaryMap,
    setPriceAlertSummaryMap,
    newsDialogOpen,
    setNewsDialogOpen,
    newsDialogSymbol,
    setNewsDialogSymbol,
    news,
    setNews,
    newsLoading,
    setNewsLoading,
    klineDialogOpen,
    setKlineDialogOpen,
    klineDialogSymbol,
    setKlineDialogSymbol,
    klineDialogMarket,
    setKlineDialogMarket,
    klineDialogName,
    setKlineDialogName,
    klineDialogHasPosition,
    setKlineDialogHasPosition,
    klineDialogInitialSummary,
    setKlineDialogInitialSummary,
    minuteDialogOpen,
    setMinuteDialogOpen,
    minuteDialogSymbol,
    setMinuteDialogSymbol,
    minuteDialogMarket,
    setMinuteDialogMarket,
    minuteDialogName,
    setMinuteDialogName,
    insightOpen,
    setInsightOpen,
    insightSymbol,
    setInsightSymbol,
    insightMarket,
    setInsightMarket,
    insightName,
    setInsightName,
    insightHasPosition,
    setInsightHasPosition,
    stockCtxMenu,
    setStockCtxMenu,
    marketStatus,
    setMarketStatus,
    klineRefreshInFlight,
    showStockForm,
    setShowStockForm,
    stockForm,
    setStockForm,
    searchQuery,
    setSearchQuery,
    searchMarket,
    setSearchMarket,
    searchResults,
    setSearchResults,
    showDropdown,
    setShowDropdown,
    searching,
    setSearching,
    refreshingStockList,
    setRefreshingStockList,
    accountDialogOpen,
    setAccountDialogOpen,
    accountForm,
    setAccountForm,
    editAccountId,
    setEditAccountId,
    positionDialogOpen,
    setPositionDialogOpen,
    positionForm,
    setPositionForm,
    editPositionId,
    setEditPositionId,
    positionDialogAccountId,
    setPositionDialogAccountId,
    positionSearchQuery,
    setPositionSearchQuery,
    positionSearchMarket,
    setPositionSearchMarket,
    positionSearchResults,
    setPositionSearchResults,
    positionSearching,
    setPositionSearching,
    showPositionDropdown,
    setShowPositionDropdown,
    positionSearchTimer,
    positionDropdownRef,
    agentDialogStock,
    setAgentDialogStock,
    deepAnalysisTarget,
    setDeepAnalysisTarget,
    triggeringAgent,
    setTriggeringAgent,
    schedulePreviewCache,
    setSchedulePreviewCache,
    schedulePreviewLoading,
    setSchedulePreviewLoading,
    runningAgents,
    setRunningAgents,
    agentResultDialog,
    setAgentResultDialog,
    stockListFilter,
    setStockListFilter,
    watchlistOnlyAlerts,
    setWatchlistOnlyAlerts,
    removeWatchStock,
    setRemoveWatchStock,
    removingWatchStock,
    setRemovingWatchStock,
    draggingWatchStockId,
    setDraggingWatchStockId,
    draggingPositionId,
    setDraggingPositionId,
    draggingPositionAccountId,
    setDraggingPositionAccountId,
    watchDragSnapshotRef,
    positionDragSnapshotRef,
  }
}
