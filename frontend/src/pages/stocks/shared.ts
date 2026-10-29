import type { SuggestionInfo, KlineSummary } from '@sida/biz-ui/components/suggestion-badge'
import { safeNum } from '@/lib/format'
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

/**
 * 行情可用性（与后端 `src/web/api/accounts.py` 的 QUOTE_* 常量同义，2026-10-29）。
 *
 * `missing`=行情源没这只票 / `invalid_price`=有字段但不可用(<=0、非数值) /
 * `not_requested`=调用方显式不要行情。前端只在自己定价（include_quotes=false + 本地行情）时
 * 覆盖成 `ok`/`missing`/`invalid_price`。
 */
export type QuoteStatus = 'ok' | 'missing' | 'invalid_price' | 'not_requested'

/**
 * 行情覆盖度（与后端 `_quotes_coverage` **同结构同语义**）。
 *
 * 合计金额只在**已定价子集**上算，故消费方展示合计时必须一并说明覆盖度：
 * `complete=false` 就代表"这个合计不是全部持仓的合计"。
 */
export interface QuotesCoverage {
  requested: boolean
  positions_total: number
  positions_priced: number | null
  positions_missing: number | null
  /** 已定价持仓的成本占全部成本的比例；总成本为 0 时为 null（未知，不是 0%）。 */
  cost_coverage_pct: number | null
  pricing_basis: QuotesPricingBasis
  /** `no_positions` 也算完整（0/0 是完整事实，不是"缺行情"）。 */
  complete: boolean
}

/**
 * 覆盖口径（后端 2026-10-29 复审收紧后的四种，互斥）：
 * `no_positions` 本来没持仓 / `all_positions` 全部有价 /
 * `priced_positions_only` 部分缺价（合计只覆盖已定价子集）/ `not_requested` 没计价。
 */
export type QuotesPricingBasis =
  | 'no_positions'
  | 'all_positions'
  | 'priced_positions_only'
  | 'not_requested'

/**
 * 当日盈亏覆盖度（与后端 `_daily_pnl_coverage` 同结构）。
 *
 * `change_pct` / 昨收缺失的持仓**算不出**当日盈亏 —— 旧实现直接跳过，于是合计
 * 悄悄少算一部分却仍显示成"今日盈亏"，读起来像"今天只赚了这么点"。
 * 现在把 `positions_observed` 显式给出来；一只都没观测到时合计为 null（未知）。
 */
export interface DailyPnlCoverage {
  requested: boolean
  positions_total: number
  positions_observed: number | null
  complete: boolean
}

/** 总资产口径（后端 `total_assets_basis`）：子集口径单独给数，不许冒充总额。 */
export type TotalAssetsBasis =
  | 'no_positions'
  | 'all_positions'
  | 'priced_positions_only'
  | 'no_priced_positions'
  | 'not_requested'


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
  /**
   * 用户自设防守线（绝对价, 原币种; Migration 223）。`null` = 用户没设 —— 后端
   * `_position_payload` / `/portfolio/summary` 都会原样下发该键，前端只许按"未设"处理，
   * 不许退回成本 ×(1-8%)（2026-10-23 产品裁决 B）。
   */
  defense_price?: number | null
  current_price: number | null
  current_price_cny: number | null  // 人民币价格（港股换算后）
  /** 行情源原值（可能是 0/坏值），仅用于区分"缺"与"坏"、排障用（后端 a2454306 起下发）。 */
  raw_current_price?: number | null
  /** `missing` / `invalid_price` / `not_requested` 表示现价不可用 —— 不是 0 元。 */
  quote_status?: QuoteStatus
  /** 现价可用（参与市值/盈亏）。false 时市值/盈亏一律 null。 */
  priced?: boolean
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
  exchange_rate: number | null  // 汇率（仅港股/美股）
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

/**
 * 账户/组合汇总（2026-10-29 缺行情契约）。
 *
 * 市值 / 盈亏 / 总资产可能是 **null = 未知**（有持仓但一只都没有可用现价），
 * 绝不当成 0、更不是 -100%。消费方一律走 `safeNum` / `safeMoney*`（它们把 null 渲染成 `--`），
 * 不许 `?? 0` 之后再算比例。
 */
export interface AccountSummary {
  id: number
  name: string
  available_funds: number
  total_market_value: number | null
  total_cost: number
  /** 已定价持仓的成本（与 total_market_value 同一篮子）。 */
  priced_cost?: number
  /** 未定价持仓的成本（只有成本是已知事实，不进盈亏分母）。 */
  unpriced_cost?: number
  total_pnl: number | null
  total_pnl_pct: number | null
  /** 当日盈亏合计；一只都没观测到（缺价或缺昨收）时为 null。 */
  total_daily_pnl: number | null
  daily_pnl_period: DailyPnlPeriod
  daily_pnl_label: string
  daily_pnl_date: string | null
  total_assets: number | null
  /** 总资产是否覆盖**全部**持仓（部分缺价 ⇒ false，此时 total_assets 为 null）。 */
  total_assets_complete?: boolean
  total_assets_basis?: TotalAssetsBasis
  /** 子集口径：已定价市值 + 全部可用资金。**不是**总资产，展示必须标注。 */
  total_assets_priced_subset?: number | null
  quotes_coverage?: QuotesCoverage
  daily_pnl_coverage?: DailyPnlCoverage
  positions: Position[]
}

export interface PortfolioTotal {
  total_market_value: number | null
  total_cost: number
  priced_cost?: number
  unpriced_cost?: number
  total_pnl: number | null
  total_pnl_pct: number | null
  total_daily_pnl: number | null
  daily_pnl_period: DailyPnlPeriod
  daily_pnl_label: string
  daily_pnl_date: string | null
  available_funds: number
  total_assets: number | null
  total_assets_complete?: boolean
  total_assets_basis?: TotalAssetsBasis
  total_assets_priced_subset?: number | null
  quotes_coverage?: QuotesCoverage
  daily_pnl_coverage?: DailyPnlCoverage
}

export interface PortfolioSummary {
  accounts: AccountSummary[]
  total: PortfolioTotal
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
    // 紧凑档给 KPI 副标签用: 「上一交易日 10-23」在 6 栏网格里会换行/被截断, 缩成「上日 10-23」
    if (meta.daily_pnl_period === 'previous_trading_day') return date ? `上日 ${date}` : '上日盈亏'
    if (meta.daily_pnl_period === 'mixed') return '最近交易日'
    return '当日'
  }
  return date && meta.daily_pnl_period === 'previous_trading_day'
    ? `${meta.daily_pnl_label} (${date})`
    : meta.daily_pnl_label
}

/** 可用现价：>0 且有限。0 / 负数 / NaN 不是成交价（停牌、坏字段），按"没有现价"处理。 */
const usablePrice = (value: unknown): number | null => {
  const n = safeNum(value)
  return n !== null && Number.isFinite(n) && n > 0 ? n : null
}

/** 汇总取值：null 保持 null（真实 0 是有效数值，不许用 falsy 判断）。 */
const roundOrNull = (value: number | null): number | null =>
  value === null ? null : round2(value)

/**
 * 覆盖度声明 —— 与后端 `_quotes_coverage` 逐字段同义（2026-10-29 复审收紧版）。
 *
 * 注意 `requested`：持仓页永远自己定价（`include_quotes=false` + 本地 WS/轮询行情），
 * 所以后端那份 `requested=false` 描述的是"后端没计价"，而本函数算的是"客户端计过价"，
 * 故这里恒为 true；覆盖度只描述**本次合并**实际定价了多少。
 *
 * `no_positions`（0 只持仓）是**完整**事实：没有持仓时"合计覆盖了全部持仓"成立，
 * 旧实现把 0 只算成 `priced_positions_only` 且 `complete=false`，UI 于是对空账户
 * 喊"有持仓缺行情"。
 */
const buildCoverage = (
  positionsTotal: number,
  positionsPriced: number,
  pricedCost: number,
  totalCost: number,
): QuotesCoverage => {
  const missing = Math.max(0, positionsTotal - positionsPriced)
  const pct = totalCost > 0 ? round2((pricedCost / totalCost) * 100) : null
  const basis: QuotesPricingBasis =
    positionsTotal === 0 ? 'no_positions' : missing === 0 ? 'all_positions' : 'priced_positions_only'
  return {
    requested: true,
    positions_total: positionsTotal,
    positions_priced: positionsPriced,
    positions_missing: missing,
    cost_coverage_pct: pct,
    pricing_basis: basis,
    complete: positionsTotal === 0 || missing === 0,
  }
}

/** 当日盈亏覆盖度（与后端 `_daily_pnl_coverage` 同结构）。 */
const buildDailyPnlCoverage = (
  positionsTotal: number,
  positionsObserved: number,
): DailyPnlCoverage => ({
  requested: true,
  positions_total: positionsTotal,
  positions_observed: Math.min(positionsObserved, positionsTotal),
  complete: positionsObserved >= positionsTotal,
})

/**
 * 总资产口径（2026-10-29 复审）—— **部分覆盖时不给总资产**。
 *
 * 「已定价市值 + 全部可用资金」在部分缺价时**不是总资产**：缺价那部分市值根本没进来，
 * 拿它当总额会把账户说得比实际小；反过来用成本顶替又会把总额说大。
 * 所以：只有全部持仓有价（或本来没持仓）才给 `total_assets`；
 * 部分覆盖时给 `null` + `total_assets_priced_subset`，展示方必须显式写「已定价子集」。
 */
const resolveTotalAssets = (
  positionsTotal: number,
  positionsPriced: number,
  pricedMarketValue: number,
  availableFunds: number,
): {
  total_assets: number | null
  total_assets_complete: boolean
  total_assets_basis: TotalAssetsBasis
  total_assets_priced_subset: number | null
} => {
  const subset = positionsPriced > 0 ? pricedMarketValue + availableFunds : null
  if (positionsTotal === 0) {
    return {
      total_assets: availableFunds,
      total_assets_complete: true,
      total_assets_basis: 'no_positions',
      total_assets_priced_subset: availableFunds,
    }
  }
  if (positionsPriced === 0) {
    return {
      total_assets: null,
      total_assets_complete: false,
      total_assets_basis: 'no_priced_positions',
      total_assets_priced_subset: null,
    }
  }
  if (positionsPriced === positionsTotal) {
    return {
      total_assets: pricedMarketValue + availableFunds,
      total_assets_complete: true,
      total_assets_basis: 'all_positions',
      total_assets_priced_subset: subset,
    }
  }
  return {
    total_assets: null,
    total_assets_complete: false,
    total_assets_basis: 'priced_positions_only',
    total_assets_priced_subset: subset,
  }
}

/**
 * 把本地行情合并进 `/portfolio/summary`。
 *
 * ## 缺行情口径（2026-10-29，与后端 a2454306 对齐）
 * 后端已改成"没有现价就没有市值"，但持仓页走 `include_quotes=false` + 本地行情**自己定价**，
 * 这里曾是真正的 -100% 出口：成本无条件全额累加进分母、市值只在有价时累加，于是
 * 「一只都没行情」= `total_pnl = 0 - 全部成本`、`total_pnl_pct = -100%`，
 * 用户看到的是一场并不存在的巨亏（局部覆盖时同理会低估）。
 *
 * 现在的口径：
 *  - **分子分母同篮子**：市值/盈亏只在已定价持仓上算，未定价持仓只贡献
 *    `unpriced_cost`（成本是已知事实），不进盈亏分母、也不记 0 涨跌；
 *  - **全缺 → 未知**：有持仓但一只都没价时，市值/盈亏/总资产给 `null`（UI 渲染 `--`），
 *    空账户才是真 `0`；
 *  - **部分覆盖显式化**：每层都带 `quotes_coverage`（与后端同结构），
 *    UI 据此说"缺行情的持仓未计入合计"，不让合计冒充全部持仓。
 */
export const mergePortfolioQuotes = (
  portfolio: PortfolioSummary | null,
  quotes: Record<string, QuoteSnapshot>
): PortfolioSummary | null => {
  if (!portfolio) return null

  const hkdRate = portfolio.exchange_rates?.HKD_CNY ?? 0.92
  const usdRate = portfolio.exchange_rates?.USD_CNY ?? 7.25

  let grandMarketValue = 0
  let grandCost = 0
  let grandPricedCost = 0
  let grandUnpricedCost = 0
  let grandPositions = 0
  let grandPriced = 0
  let grandDailyObserved = 0
  let grandAvailable = 0
  let grandDailyPnl = 0
  const grandDailyPnlObservations: Array<{ period: DailyPnlPeriod; date: string | null }> = []

  const accounts = portfolio.accounts.map(account => {
    let accMarketValue = 0
    let accCost = 0
    let accPricedCost = 0
    let accUnpricedCost = 0
    let accPriced = 0
    let accDailyObserved = 0
    let accDailyPnl = 0
    const accDailyPnlObservations: Array<{ period: DailyPnlPeriod; date: string | null }> = []

    const positions = account.positions.map(pos => {
      const quote = quotes[`${pos.market}:${pos.symbol}`]
      const quoteTime = quote?.quote_time ?? pos.quote_time ?? null
      const quoteDate = quote?.quote_date ?? pos.quote_date ?? null
      const daily_pnl_period = quote?.daily_pnl_period ?? pos.daily_pnl_period ?? 'unknown'
      const rate = pos.market === 'HK' ? hkdRate : pos.market === 'US' ? usdRate : 1

      // 现价：本地行情优先，其次后端已下发的持仓价（include_quotes=true 时才会有）。
      // 两者都不可用 ⇒ 没有现价；`raw` 保留原值以便区分"缺"与"坏"。
      const quotePrice = quote?.current_price ?? null
      const heldPrice = pos.current_price ?? null
      const rawPrice = quotePrice ?? heldPrice
      const current_price = usablePrice(quotePrice) ?? usablePrice(heldPrice)
      const priced = current_price !== null
      const quote_status: QuoteStatus = priced
        ? 'ok'
        : rawPrice === null || rawPrice === undefined
          ? 'missing'
          : 'invalid_price'
      const change_pct = priced ? (quote?.change_pct ?? pos.change_pct ?? null) : null

      const costPrice = safeNum(pos.cost_price) ?? 0
      const quantity = safeNum(pos.quantity) ?? 0
      const cost = costPrice * quantity * rate
      accCost += cost
      if (!priced) accUnpricedCost += cost

      let market_value: number | null = null
      let market_value_cny: number | null = null
      let pnl: number | null = null
      let pnl_pct: number | null = null
      let daily_pnl: number | null = null
      let daily_pnl_pct: number | null = null

      if (priced) {
        market_value = current_price * quantity
        market_value_cny = market_value * rate
        accMarketValue += market_value_cny
        accPricedCost += cost
        accPriced += 1
        // 成本 <= 0 时百分比是未知，不是 0%（后端同样给 None）
        pnl = market_value_cny - cost
        pnl_pct = cost > 0 ? (pnl / cost) * 100 : null
      }

      if (priced && change_pct != null && change_pct !== -100) {
        const prev = current_price / (1 + change_pct / 100)
        if (isFinite(prev) && prev > 0) {
          daily_pnl = round2((current_price - prev) * quantity * rate)
          daily_pnl_pct = round2(change_pct)
          accDailyPnl += daily_pnl
          // 只有"有现价 + 有可用的涨跌幅/昨收"的持仓才被观测到；缺一个就不是 0 涨跌。
          accDailyObserved += 1
          const observation = { period: daily_pnl_period, date: quoteDate }
          accDailyPnlObservations.push(observation)
          grandDailyPnlObservations.push(observation)
        }
      }

      return {
        ...pos,
        current_price,
        raw_current_price: rawPrice,
        quote_status,
        priced,
        current_price_cny: priced ? round2(current_price * rate) : null,
        change_pct,
        market_value: roundOrNull(market_value),
        market_value_cny: roundOrNull(market_value_cny),
        pnl: roundOrNull(pnl),
        pnl_pct: roundOrNull(pnl_pct),
        daily_pnl,
        daily_pnl_pct,
        daily_pnl_period,
        quote_time: quoteTime,
        quote_date: quoteDate,
        exchange_rate: pos.market === 'HK' || pos.market === 'US' ? rate : null,
      }
    })

    const positionsCount = account.positions.length
    let accMarketValueOut: number | null
    let accPnl: number | null
    let accPnlPct: number | null
    let accDailyPnlOut: number | null
    if (accPriced > 0) {
      accMarketValueOut = accMarketValue
      accPnl = accMarketValue - accPricedCost
      accPnlPct = accPricedCost > 0 ? (accPnl / accPricedCost) * 100 : null
      // 一只都没观测到（缺涨跌幅/昨收）⇒ 未知，不许拿 0 冒充"今天没赚没亏"。
      accDailyPnlOut = accDailyObserved > 0 ? round2(accDailyPnl) : null
    } else if (positionsCount > 0) {
      // 有持仓但一只都没有可用现价：市值/盈亏/总资产/当日盈亏都是未知。
      accMarketValueOut = null
      accPnl = null
      accPnlPct = null
      accDailyPnlOut = null
    } else {
      // 空账户：0 只持仓就是真 0 盈亏。
      accMarketValueOut = 0
      accPnl = 0
      accPnlPct = 0
      accDailyPnlOut = 0
    }
    const accAssets = resolveTotalAssets(
      positionsCount,
      accPriced,
      accMarketValue,
      account.available_funds,
    )

    grandMarketValue += accMarketValue
    grandCost += accCost
    grandPricedCost += accPricedCost
    grandUnpricedCost += accUnpricedCost
    grandPositions += positionsCount
    grandPriced += accPriced
    grandDailyObserved += accDailyObserved
    grandAvailable += account.available_funds
    grandDailyPnl += accDailyPnl

    return {
      ...account,
      total_market_value: roundOrNull(accMarketValueOut),
      total_cost: round2(accCost),
      priced_cost: round2(accPricedCost),
      unpriced_cost: round2(accUnpricedCost),
      total_pnl: roundOrNull(accPnl),
      total_pnl_pct: roundOrNull(accPnlPct),
      total_daily_pnl: roundOrNull(accDailyPnlOut),
      total_assets: roundOrNull(accAssets.total_assets),
      total_assets_complete: accAssets.total_assets_complete,
      total_assets_basis: accAssets.total_assets_basis,
      total_assets_priced_subset: roundOrNull(accAssets.total_assets_priced_subset),
      quotes_coverage: buildCoverage(positionsCount, accPriced, accPricedCost, accCost),
      daily_pnl_coverage: buildDailyPnlCoverage(positionsCount, accDailyObserved),
      ...summarizeDailyPnlPeriod(accDailyPnlObservations),
      positions,
    }
  })

  let grandMarketValueOut: number | null
  let grandPnl: number | null
  let grandPnlPct: number | null
  let grandDailyPnlOut: number | null
  if (grandPriced > 0) {
    grandMarketValueOut = grandMarketValue
    grandPnl = grandMarketValue - grandPricedCost
    grandPnlPct = grandPricedCost > 0 ? (grandPnl / grandPricedCost) * 100 : null
    grandDailyPnlOut = grandDailyObserved > 0 ? round2(grandDailyPnl) : null
  } else if (grandPositions > 0) {
    grandMarketValueOut = null
    grandPnl = null
    grandPnlPct = null
    grandDailyPnlOut = null
  } else {
    grandMarketValueOut = 0
    grandPnl = 0
    grandPnlPct = 0
    grandDailyPnlOut = 0
  }
  const grandAssets = resolveTotalAssets(
    grandPositions,
    grandPriced,
    grandMarketValue,
    grandAvailable,
  )

  return {
    ...portfolio,
    accounts,
    total: {
      total_market_value: roundOrNull(grandMarketValueOut),
      total_cost: round2(grandCost),
      priced_cost: round2(grandPricedCost),
      unpriced_cost: round2(grandUnpricedCost),
      total_pnl: roundOrNull(grandPnl),
      total_pnl_pct: roundOrNull(grandPnlPct),
      total_daily_pnl: roundOrNull(grandDailyPnlOut),
      available_funds: round2(grandAvailable),
      total_assets: roundOrNull(grandAssets.total_assets),
      total_assets_complete: grandAssets.total_assets_complete,
      total_assets_basis: grandAssets.total_assets_basis,
      total_assets_priced_subset: roundOrNull(grandAssets.total_assets_priced_subset),
      quotes_coverage: buildCoverage(grandPositions, grandPriced, grandPricedCost, grandCost),
      daily_pnl_coverage: buildDailyPnlCoverage(grandPositions, grandDailyObserved),
      ...summarizeDailyPnlPeriod(grandDailyPnlObservations),
    },
  }
}

/**
 * 仓位比例（持仓市值 / 总资产）。
 *
 * 总市值或总资产为 **null（未知，缺行情）** 时比例也是未知 —— 不许 `|| 0` 之后再算，
 * 那会把"不知道"渲染成 `0.0%`，读起来像"空仓"。
 */
export const computePositionRatio = (total: {
  total_market_value?: number | null
  total_assets?: number | null
  available_funds?: number | null
}): { mv: number | null; assets: number | null; pct: number | null } => {
  const mv = safeNum(total.total_market_value)
  const assets = safeNum(total.total_assets)
  const pct = mv !== null && assets !== null && assets > 0 ? (mv / assets) * 100 : null
  return { mv, assets, pct }
}
