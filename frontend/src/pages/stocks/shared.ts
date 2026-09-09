import type { SuggestionInfo, KlineSummary } from '@panwatch/biz-ui/components/suggestion-badge'
/** W5.3: Stocks 页的类型/常量/纯函数(从 Stocks.tsx 逐字抽出)。 */

export interface AgentResult {
  success?: boolean
  message?: string
  title: string
  content: string
  should_alert: boolean
  notified: boolean
  skipped?: boolean
}

export interface StockAgentInfo {
  agent_name: string
  schedule: string
  ai_model_id: number | null
  notify_channel_ids: number[]
}

export interface Stock {
  id: number
  symbol: string
  name: string
  market: string
  sort_order?: number
  agents: StockAgentInfo[]
}

export interface Account {
  id: number
  name: string
  available_funds: number
  enabled: boolean
}

export interface Position {
  id: number
  stock_id: number
  sort_order?: number
  symbol: string
  name: string
  market: string
  cost_price: number
  quantity: number
  invested_amount: number | null
  trading_style: string  // short: 短线, swing: 波段, long: 长线
  current_price: number | null
  current_price_cny: number | null  // 人民币价格（港股换算后）
  change_pct: number | null
  market_value: number | null
  market_value_cny: number | null  // 人民币市值
  pnl: number | null
  pnl_pct: number | null
  daily_pnl: number | null
  daily_pnl_pct: number | null
  daily_pnl_period: DailyPnlPeriod
  quote_time: string | null
  quote_date: string | null
  exchange_rate: number | null  // 汇率（仅港股）
}

export type DailyPnlPeriod = 'today' | 'previous_trading_day' | 'mixed' | 'unknown'

export interface DailyPnlMeta {
  daily_pnl_period: DailyPnlPeriod
  daily_pnl_label: string
  daily_pnl_date: string | null
}

export interface QuoteSnapshot {
  current_price: number | null
  change_pct: number | null
  quote_time: string | null
  quote_date: string | null
  daily_pnl_period: DailyPnlPeriod
}

export interface AccountSummary {
  id: number
  name: string
  available_funds: number
  total_market_value: number
  total_cost: number
  total_pnl: number
  total_pnl_pct: number
  total_daily_pnl: number
  daily_pnl_period: DailyPnlPeriod
  daily_pnl_label: string
  daily_pnl_date: string | null
  total_assets: number
  positions: Position[]
}

export interface PortfolioSummary {
  accounts: AccountSummary[]
  total: {
    total_market_value: number
    total_cost: number
    total_pnl: number
    total_pnl_pct: number
    total_daily_pnl: number
    daily_pnl_period: DailyPnlPeriod
    daily_pnl_label: string
    daily_pnl_date: string | null
    available_funds: number
    total_assets: number
  }
  exchange_rates?: {
    HKD_CNY: number
    USD_CNY?: number
  }
  quotes?: Record<string, QuoteSnapshot>
}

export interface AgentConfig {
  name: string
  display_name: string
  description: string
  enabled: boolean
  schedule: string
  execution_mode: string  // batch: 批量分析, single: 逐只分析
}

export interface SchedulePreview {
  schedule: string
  timezone: string
  next_runs: string[]
}

export interface SearchResult {
  symbol: string
  name: string
  market: string
}

export interface QuoteRequestItem {
  symbol: string
  market: string
}

export interface QuoteResponse {
  symbol: string
  market: string
  current_price: number | null
  change_pct: number | null
  quote_time: string | null
  quote_date: string | null
  daily_pnl_period: DailyPnlPeriod
}

export interface StockForm {
  symbol: string
  name: string
  market: string
}

export interface AccountForm {
  name: string
  available_funds: string
}

export interface PositionForm {
  account_id: number
  stock_id: number
  cost_price: string
  quantity: string
  invested_amount: string
  trading_style: string
  // 搜索选中的股票信息（新增持仓时用）
  stock_symbol: string
  stock_name: string
  stock_market: string
}

// 股票建议信息（来自盘中监控 API）
export interface StockSuggestionData {
  symbol: string
  suggestion: SuggestionInfo | null
  kline: KlineSummary | null
}

// 建议池中的建议（包含来源和时间信息）
export interface PoolSuggestion {
  id: number
  stock_symbol: string
  stock_market?: string
  stock_name: string
  action: string
  action_label: string
  signal: string
  reason: string
  agent_name: string
  agent_label: string
  created_at: string
  expires_at: string | null
  is_expired: boolean
  prompt_context: string
  ai_response: string
  meta?: Record<string, any>
  should_alert?: boolean
}

export interface MarketStatus {
  code: string
  name: string
  status: string
  status_text: string
  is_trading: boolean
  sessions: string[]
  local_time: string
}

export interface NewsItem {
  source: string
  source_label: string
  external_id: string
  title: string
  content: string
  publish_time: string
  symbols: string[]
  importance: number
  url: string
}

export interface PriceAlertRuleSummary {
  stock_symbol: string
  market: string
  enabled: boolean
}

export const emptyStockForm: StockForm = { symbol: '', name: '', market: 'CN' }
export const emptyAccountForm: AccountForm = { name: '', available_funds: '0' }

export const round2 = (value: number) => Math.round(value * 100) / 100

export const summarizeDailyPnlPeriod = (
  observations: Array<{ period: DailyPnlPeriod; date: string | null }>,
): DailyPnlMeta => {
  if (observations.length === 0) {
    return { daily_pnl_period: 'unknown', daily_pnl_label: '当日盈亏', daily_pnl_date: null }
  }
  const periods = new Set(observations.map(item => item.period))
  const dates = new Set(observations.map(item => item.date).filter((value): value is string => !!value))
  if (periods.size === 1 && periods.has('today')) {
    return {
      daily_pnl_period: 'today',
      daily_pnl_label: '今日盈亏',
      daily_pnl_date: dates.size === 1 ? [...dates][0] : null,
    }
  }
  if (periods.size === 1 && periods.has('previous_trading_day') && dates.size === 1) {
    return {
      daily_pnl_period: 'previous_trading_day',
      daily_pnl_label: '上一交易日盈亏',
      daily_pnl_date: [...dates][0],
    }
  }
  return {
    daily_pnl_period: periods.size > 1 || dates.size > 1 ? 'mixed' : 'unknown',
    daily_pnl_label: dates.size > 0 ? '最近交易日盈亏' : '当日盈亏',
    daily_pnl_date: null,
  }
}

export const dailyPnlDisplayLabel = (meta: DailyPnlMeta, compact = false): string => {
  const date = meta.daily_pnl_date ? meta.daily_pnl_date.slice(5) : ''
  if (compact) {
    if (meta.daily_pnl_period === 'today') return '今日'
    if (meta.daily_pnl_period === 'previous_trading_day') return date ? `上一交易日 ${date}` : '上一交易日'
    if (meta.daily_pnl_period === 'mixed') return '最近交易日'
    return '当日'
  }
  return date && meta.daily_pnl_period === 'previous_trading_day'
    ? `${meta.daily_pnl_label} (${date})`
    : meta.daily_pnl_label
}

export const mergePortfolioQuotes = (
  portfolio: PortfolioSummary | null,
  quotes: Record<string, QuoteSnapshot>
): PortfolioSummary | null => {
  if (!portfolio) return null

  const hkdRate = portfolio.exchange_rates?.HKD_CNY ?? 0.92
  const usdRate = portfolio.exchange_rates?.USD_CNY ?? 7.25

  let grandMarketValue = 0
  let grandCost = 0
  let grandAvailable = 0
  let grandDailyPnl = 0
  const grandDailyPnlObservations: Array<{ period: DailyPnlPeriod; date: string | null }> = []

  const accounts = portfolio.accounts.map(account => {
    let accMarketValue = 0
    let accCost = 0
    let accDailyPnl = 0
    const accDailyPnlObservations: Array<{ period: DailyPnlPeriod; date: string | null }> = []

    const positions = account.positions.map(pos => {
      const quote = quotes[`${pos.market}:${pos.symbol}`]
      const current_price = quote?.current_price ?? pos.current_price ?? null
      const change_pct = quote?.change_pct ?? pos.change_pct ?? null
      const quote_time = quote?.quote_time ?? pos.quote_time ?? null
      const quote_date = quote?.quote_date ?? pos.quote_date ?? null
      const daily_pnl_period = quote?.daily_pnl_period ?? pos.daily_pnl_period ?? 'unknown'
      const rate = pos.market === 'HK' ? hkdRate : pos.market === 'US' ? usdRate : 1

      const cost = pos.cost_price * pos.quantity * rate
      accCost += cost

      let market_value: number | null = null
      let market_value_cny: number | null = null
      let pnl: number | null = null
      let pnl_pct: number | null = null
      let daily_pnl: number | null = null
      let daily_pnl_pct: number | null = null

      if (current_price != null) {
        market_value = current_price * pos.quantity
        market_value_cny = market_value * rate
        accMarketValue += market_value_cny
        pnl = market_value_cny - cost
        pnl_pct = cost > 0 ? (pnl / cost * 100) : 0
      }

      if (current_price != null && change_pct != null && change_pct !== -100) {
        const prev = current_price / (1 + change_pct / 100)
        if (isFinite(prev) && prev > 0) {
          daily_pnl = round2((current_price - prev) * pos.quantity * rate)
          daily_pnl_pct = round2(change_pct)
          accDailyPnl += daily_pnl
          const observation = { period: daily_pnl_period, date: quote_date }
          accDailyPnlObservations.push(observation)
          grandDailyPnlObservations.push(observation)
        }
      }

      return {
        ...pos,
        current_price,
        current_price_cny: current_price != null ? current_price * rate : null,
        change_pct,
        market_value,
        market_value_cny,
        pnl,
        pnl_pct,
        daily_pnl,
        daily_pnl_pct,
        daily_pnl_period,
        quote_time,
        quote_date,
        exchange_rate: pos.market === 'HK' || pos.market === 'US' ? rate : null,
      }
    })

    const accPnl = accMarketValue - accCost
    const accPnlPct = accCost > 0 ? (accPnl / accCost * 100) : 0
    const accTotalAssets = accMarketValue + account.available_funds

    grandMarketValue += accMarketValue
    grandCost += accCost
    grandAvailable += account.available_funds
    grandDailyPnl += accDailyPnl

    return {
      ...account,
      total_market_value: round2(accMarketValue),
      total_cost: round2(accCost),
      total_pnl: round2(accPnl),
      total_pnl_pct: round2(accPnlPct),
      total_daily_pnl: round2(accDailyPnl),
      total_assets: round2(accTotalAssets),
      ...summarizeDailyPnlPeriod(accDailyPnlObservations),
      positions,
    }
  })

  const grandPnl = grandMarketValue - grandCost
  const grandPnlPct = grandCost > 0 ? (grandPnl / grandCost * 100) : 0
  const grandTotalAssets = grandMarketValue + grandAvailable

  return {
    ...portfolio,
    accounts,
    total: {
      total_market_value: round2(grandMarketValue),
      total_cost: round2(grandCost),
      total_pnl: round2(grandPnl),
      total_pnl_pct: round2(grandPnlPct),
      total_daily_pnl: round2(grandDailyPnl),
      available_funds: round2(grandAvailable),
      total_assets: round2(grandTotalAssets),
      ...summarizeDailyPnlPeriod(grandDailyPnlObservations),
    },
  }
}
