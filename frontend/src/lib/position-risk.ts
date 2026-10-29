/**
 * 持仓风险检查（P2 持仓风险切片, 2026-10-29）—— 纯函数，无 React、无请求。
 *
 * ## 为什么要有这一层
 * 持仓页只有**组合合计**（总市值/总盈亏），逐只的「有没有纪律位、离纪律位多远、哪只
 * 压根没有现价」散在三处：`DefenseLineBanner`（只在击穿时弹一条）、`AccountsSection`
 * （表格里没有防线列）、`/portfolio/diagnostics`（只有集中度，且无前端消费者）。
 * 这一层把同一份 `/portfolio/summary` 读数折成**逐账户可核对的检查清单**，供持仓页与
 * 参谋卡片共用一份判定，不再各写一套阈值。
 *
 * ## 口径（每条都对应一次真实事故）
 * 1. **破位只认用户自设防线**（2026-10-23 产品裁决 B）：`positions.defense_price` 为空
 *    ⇒ `unset`，既不是安全也不是破位。系统不许拿 `成本 × (1-8%)` 冒充用户纪律位。
 * 2. **缺失不许静默**：`current_price` 为空 ⇒ `unknown`，并从 `noPrice` 里数出来。把
 *    缺行情的持仓当「安全」等于替用户判了个没有数据支撑的结论。
 * 3. **`price === defense` 算破位**（`<=`），与 `DefenseLineBanner` 同源；
 *    「逼近」是半开区间 `(0, NEAR_LINE_PCT]`，正好 3% 仍算逼近。
 * 4. **PG DECIMAL 经 JSON 是字符串**：所有数值一律走 `safeNum`，不许 `"100" <= "99"` 的
 *    字典序比较（那会把安全判成破位）。
 * 5. **只产出观测值**：状态 / 距离 / 缺口。不产出方向、买卖、目标价 —— 结论交给人。
 *
 * ## 2026-10-29 二次评审修的四类错判
 * 6. **同标的多账户各自一条防线，不合并**：旧实现把同一 `market:symbol` 的多账户合并成一行，
 *    且 `prev.defense = defense` 按**遍历顺序**覆盖 —— 主账户 34 / 备用账户 25 / 现价 33 时，
 *    清单和告警条会随账户顺序在「破位」和「安全」之间翻。防线是**用户对某个账户的**纪律位，
 *    不是标的属性：这里改为一行 = 一个账户的持仓，账户归属显式带出，冲突只**标注**不消除。
 * 7. **旧报价不许冒充实时破位**：`daily_pnl_period` 不是 `today` 时（后端 `classify_quote_period`
 *    对任何早于今天的日期都只给 `previous_trading_day`，可能是隔日也可能是隔周），
 *    一律 `stale`，不进 `breached`/`near` 计数，UI 显示真实报价日期与时间。
 *    前端**不自推交易日历**：只认后端判好的 `daily_pnl_period`。
 * 8. **全盈利不许叫「浮亏最重」**：`worstByPnl` 只是「按盈亏最弱的一只」（可能是正收益），
 *    `allProfitable` 交给 UI 换措辞。
 * 9. **缺口独立计数**：既无现价又未设防线时，`noPrice` 与 `noDefense` 各算一次 ——
 *    旧实现用状态桶计数，后一个缺口被前一个吃掉。
 * 10. **币种按市场**：HK→HKD、US→USD、CN→CNY；一张表里混排三种币却不标币种，
 *     读起来就是「腾讯 380 比茅台 1500 便宜」。
 * 11. **缺成本不稀释**：旧合并把「无成本账户的数量」也算进加权分母，摊薄了另一账户的成本。
 *     现在每行独立，缺失直接 `no_cost` + `pnlPct=null`。
 */

import { safeFixed, safeNum, safePrice } from './format'

/** 距防线多近算「逼近」。与 `DefenseLineBanner` 同源（该组件已改为 import 本常量）。 */
export const NEAR_LINE_PCT = 3

/**
 * 防线结论。`stale` = 有价也有防线，但报价无法证明是当日的 —— 此时**不下**破位/安全结论。
 */
export type DefenseState = 'breached' | 'near' | 'safe' | 'unset' | 'unknown' | 'stale'

/** 数据缺口（**不是**风险结论）：缺现价 / 缺用户防线 / 缺成本。 */
export type RiskGap = 'no_price' | 'no_defense' | 'no_cost'

/** 行情新鲜度：`current` 仅当后端 `daily_pnl_period === 'today'`。 */
export type QuoteFreshness = 'current' | 'stale' | 'unknown' | 'missing'

export type PricePeriod = 'today' | 'previous_trading_day' | 'mixed' | 'unknown'

export type Currency = 'CNY' | 'HKD' | 'USD' | 'UNKNOWN'

/** `/portfolio/summary` 持仓行的最小子集；数值允许是字符串（PG DECIMAL）。 */
export interface PositionRiskInput {
  id?: number | string | null
  /** 账户归属（同一 symbol 在不同账户是两条独立的防线）。 */
  account_id?: number | string | null
  account_name?: string | null
  symbol: string
  name?: string | null
  market?: string | null
  quantity?: number | string | null
  cost_price?: number | string | null
  current_price?: number | string | null
  pnl_pct?: number | string | null
  defense_price?: number | string | null
  quote_date?: string | null
  quote_time?: string | null
  daily_pnl_period?: string | null
}

export interface PositionRiskRow {
  /** 账户内唯一：`accountId:symbol`（无账户信息时退化为 `market:symbol`）。 */
  key: string
  accountId: string | null
  accountName: string | null
  symbol: string
  name: string
  market: string
  currency: Currency
  costPrice: number | null
  currentPrice: number | null
  /** 后端给的浮亏百分比；缺价/缺成本时为 null，不回退成 0。 */
  pnlPct: number | null
  defensePrice: number | null
  defenseState: DefenseState
  /** `(现价 - 防线) / 防线 * 100`；有价有防线时非 null（stale 也保留，供核对）。 */
  distanceToDefensePct: number | null
  quoteDate: string | null
  quoteTime: string | null
  pricePeriod: PricePeriod
  freshness: QuoteFreshness
  /** 同标的多账户防线不一致（本行仍按自己的防线判定，不做任何合并）。 */
  sharedSymbolConflict: boolean
  gaps: RiskGap[]
}

export interface PositionRiskReport {
  /** 持仓行数（**每个账户各算一行**，不做跨账户合并）。 */
  total: number
  /** 去重后的标的数（`market:symbol`）。 */
  symbols: number
  /** 账户数。 */
  accounts: number
  /** 有可用现价的持仓行数 —— 分母是它，不是 total。 */
  priced: number
  breached: number
  near: number
  safe: number
  /** 有价有防线，但报价不是当日行情 —— 未计入破位/逼近。 */
  stale: number
  /** 没有可用现价（连防线都不判）—— 与 `noPrice` 缺口同源但这里是状态桶。 */
  unknown: number
  /** 有现价但用户没设防线（不告警，当然也不判破位）。 */
  unset: number
  noDefense: number
  noPrice: number
  noCost: number
  /** 按盈亏最弱的一只（可能为正）；没有可比对象时为 null。 */
  worstByPnl: PositionRiskRow | null
  /** 按盈亏最强的一只。 */
  bestByPnl: PositionRiskRow | null
  /** 所有有盈亏的持仓都为正 —— UI 据此换掉「浮亏最重」的说法。 */
  allProfitable: boolean
  /** 同一标的多账户防线不同（已按账户分行，不合并、不取首个）。 */
  conflictingSymbols: string[]
  /** 已按处置优先级排序：破位（越深越前）→ 逼近 → 行情滞后 → 缺现价 → 未设防线 → 安全。 */
  rows: PositionRiskRow[]
}

const PERIODS: ReadonlySet<string> = new Set(['today', 'previous_trading_day', 'mixed', 'unknown'])

const CURRENCY_BY_MARKET: Record<string, Currency> = {
  CN: 'CNY',
  HK: 'HKD',
  US: 'USD',
}

const CURRENCY_SYMBOL: Record<Currency, string> = {
  CNY: '¥',
  HKD: 'HK$',
  USD: 'US$',
  UNKNOWN: '',
}

const STATE_RANK: Record<DefenseState, number> = {
  breached: 0,
  near: 1,
  stale: 2,
  unknown: 3,
  unset: 4,
  safe: 5,
}

/** 正数才有效（价格/成本/防线为 0 或负数都是"没有这个数"，不是 0）。 */
function positive(v: unknown): number | null {
  const n = safeNum(v)
  return n !== null && n > 0 ? n : null
}

const normalizeMarket = (market: unknown): string =>
  String(market ?? 'CN').trim().toUpperCase() || 'CN'

/** 按市场取币种；未知市场给 UNKNOWN（**不**默认成人民币）。 */
export function currencyForMarket(market: unknown): Currency {
  return CURRENCY_BY_MARKET[normalizeMarket(market)] ?? 'UNKNOWN'
}

/**
 * 价格带币种前缀。港股/美股必须标出来 —— 否则同一张表里的 380（HKD）和 1500（CNY）
 * 会被当成同一把尺子比较。
 */
export function formatPriceWithCurrency(price: unknown, market: unknown): string {
  const n = safeNum(price)
  if (n === null) return '--'
  // 价格精度走 safePrice（R6 门禁：禁裸 toFixed）
  return `${CURRENCY_SYMBOL[currencyForMarket(market)]}${safePrice(n, 4)}`
}

/**
 * 单只持仓的防线判定 —— **几何口径的唯一实现**（不看行情新鲜度）。
 *
 * `DefenseLineBanner` 曾自己写了一份 `price <= defense` / `distance <= 3`，两处并存时
 * 任何一边调阈值都会让「告警条没响、体检清单说破位」打架，故收敛到这里。
 */
export function evaluateDefenseLine(
  price: unknown,
  defense: unknown,
): { state: DefenseState; distancePct: number | null } {
  const p = positive(price)
  const d = positive(defense)
  if (p === null) return { state: 'unknown', distancePct: null }
  if (d === null) return { state: 'unset', distancePct: null }
  const distancePct = ((p - d) / d) * 100
  if (p <= d) return { state: 'breached', distancePct }
  if (distancePct <= NEAR_LINE_PCT) return { state: 'near', distancePct }
  return { state: 'safe', distancePct }
}

/** 新鲜度只由后端 `daily_pnl_period` 决定（前端不自推交易日历、不读系统时钟）。 */
export function freshnessFor(price: unknown, period: unknown): QuoteFreshness {
  if (positive(price) === null) return 'missing'
  const p = String(period ?? '')
  if (p === 'today') return 'current'
  if (p === 'previous_trading_day') return 'stale'
  return 'unknown'
}

/**
 * 防线结论 + 新鲜度闸门 —— **全站唯一实现**（`DefenseLineBanner` 与持仓清单共用）。
 *
 * 报价无法证明是当日（`stale` / `unknown`）时不给破位/逼近/安全结论：
 * 旧报价当实时价判破位，等于拿隔日甚至隔周的价位说"你现在已经跌破纪律位"。
 * 距离仍返回（可核对），但状态是 `stale`。
 */
export function resolveDefenseState(args: {
  price: unknown
  defense: unknown
  period?: string | null
}): { state: DefenseState; distancePct: number | null; freshness: QuoteFreshness } {
  const freshness = freshnessFor(args.price, args.period)
  if (freshness === 'missing') {
    return { state: 'unknown', distancePct: null, freshness }
  }
  const geo = evaluateDefenseLine(args.price, args.defense)
  if (geo.state === 'unset') return { ...geo, freshness }
  if (freshness !== 'current') {
    return { state: 'stale', distancePct: geo.distancePct, freshness }
  }
  return { ...geo, freshness }
}

/**
 * 报价距今多久（**可验证新鲜度**）。
 *
 * `now` 必须由调用方注入：库内读系统时钟会让测试不可重复，也会掩盖"没拿到时间"。
 * 无法解析 / 未来时间（跨市场时区没对齐时会出现）一律 null —— 宁可不显示，也不编一个时点。
 */
export function quoteAgeLabel(quoteTime: unknown, now?: Date | null): string | null {
  if (!(now instanceof Date) || Number.isNaN(now.getTime())) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(
    String(quoteTime ?? '').trim(),
  )
  if (!m) return null
  const then = new Date(
    Number(m[1]),
    Number(m[2]) - 1,
    Number(m[3]),
    Number(m[4]),
    Number(m[5]),
    Number(m[6] ?? 0),
  )
  const diffMs = now.getTime() - then.getTime()
  if (!Number.isFinite(diffMs) || diffMs < 0) return null
  const minutes = diffMs / 60000
  if (minutes < 60) return `${Math.round(minutes)} 分钟前`
  const hours = minutes / 60
  if (hours < 36) return `${safeFixed(hours, 1)} 小时前`
  return `${safeFixed(hours / 24, 1)} 天前`
}

/** 把一行持仓读数折成风险行（每账户一行，绝不跨账户合并防线/成本）。 */
export function rowFromPosition(p: PositionRiskInput): PositionRiskRow {
  const symbol = String(p.symbol ?? '').trim()
  const market = normalizeMarket(p.market)
  const accountId =
    p.account_id === null || p.account_id === undefined || String(p.account_id).trim() === ''
      ? null
      : String(p.account_id)
  const price = positive(p.current_price)
  const defense = positive(p.defense_price)
  const cost = positive(p.cost_price)
  const rawPeriod = String(p.daily_pnl_period ?? '')
  const pricePeriod: PricePeriod = (PERIODS.has(rawPeriod) ? rawPeriod : 'unknown') as PricePeriod
  const { state, distancePct, freshness } = resolveDefenseState({
    price,
    defense,
    period: pricePeriod,
  })

  const gaps: RiskGap[] = []
  if (price === null) gaps.push('no_price')
  if (defense === null) gaps.push('no_defense')
  if (cost === null) gaps.push('no_cost')

  // 有价有成本才谈盈亏：缺价时后端即便残留一个旧 pnl_pct 也不许展示成"当前浮亏"。
  const backendPnl = safeNum(p.pnl_pct)
  const pnlPct =
    price !== null && cost !== null
      ? backendPnl !== null
        ? backendPnl
        : ((price - cost) / cost) * 100
      : null

  return {
    key: accountId !== null ? `${accountId}:${market}:${symbol}` : `${market}:${symbol}`,
    accountId,
    accountName:
      p.account_name === null || p.account_name === undefined
        ? null
        : String(p.account_name).trim() || null,
    symbol,
    name: String(p.name ?? symbol).trim() || symbol,
    market,
    currency: currencyForMarket(market),
    costPrice: cost,
    currentPrice: price,
    pnlPct,
    defensePrice: defense,
    defenseState: state,
    distanceToDefensePct: distancePct,
    quoteDate: p.quote_date ?? null,
    quoteTime: p.quote_time ?? null,
    pricePeriod,
    freshness,
    sharedSymbolConflict: false,
    gaps,
  }
}

/**
 * 把 `/portfolio/summary` 的持仓行折成风险检查报告。
 *
 * 纯函数：不请求、不读时间（时点用后端已判好的 `daily_pnl_period` + `quote_date`，
 * 前端自己判「今天」会在非交易时段把上一交易日的读数标成当日）。
 */
export function buildPositionRiskReport(
  positions: PositionRiskInput[] | null | undefined,
): PositionRiskReport {
  const rows = (Array.isArray(positions) ? positions : [])
    .filter((p) => String(p.symbol ?? '').trim() !== '')
    .map(rowFromPosition)

  // 同一标的多账户防线不一致：只**标注**，不合并、不挑一条 —— 防线是账户级的。
  const defensesBySymbol = new Map<string, Set<string>>()
  for (const row of rows) {
    const k = `${row.market}:${row.symbol}`
    const set = defensesBySymbol.get(k) ?? new Set<string>()
    set.add(row.defensePrice === null ? '∅' : String(row.defensePrice))
    defensesBySymbol.set(k, set)
  }
  const conflictingSymbols = [...defensesBySymbol.entries()]
    .filter(([, set]) => set.size > 1)
    .map(([k]) => k)
  const conflictSet = new Set(conflictingSymbols)
  for (const row of rows) {
    row.sharedSymbolConflict = conflictSet.has(`${row.market}:${row.symbol}`)
  }

  rows.sort((a, b) => {
    const rank = STATE_RANK[a.defenseState] - STATE_RANK[b.defenseState]
    if (rank !== 0) return rank
    if (a.defenseState === 'breached' || a.defenseState === 'near') {
      // 越靠近/越深越靠前（distance 越小越危险）
      return (a.distanceToDefensePct ?? 0) - (b.distanceToDefensePct ?? 0)
    }
    // 其余按浮亏从重到轻，缺浮亏的排后面
    const ap = a.pnlPct
    const bp = b.pnlPct
    if (ap === null && bp !== null) return 1
    if (ap !== null && bp === null) return -1
    if (ap !== null && bp !== null && ap !== bp) return ap - bp
    // 同标的多账户：按账户名稳定排序，避免随输入顺序抖动
    const ac = String(a.accountName ?? '')
    const bc = String(b.accountName ?? '')
    if (ac !== bc) return ac.localeCompare(bc)
    return a.key.localeCompare(b.key)
  })

  const withPnl = rows.filter((r) => r.pnlPct !== null)
  let worstByPnl: PositionRiskRow | null = null
  let bestByPnl: PositionRiskRow | null = null
  for (const r of withPnl) {
    if (worstByPnl === null || (r.pnlPct as number) < (worstByPnl.pnlPct as number)) worstByPnl = r
    if (bestByPnl === null || (r.pnlPct as number) > (bestByPnl.pnlPct as number)) bestByPnl = r
  }

  const symbols = new Set(rows.map((r) => `${r.market}:${r.symbol}`))
  const accounts = new Set(rows.map((r) => r.accountId ?? '∅'))

  return {
    total: rows.length,
    symbols: symbols.size,
    accounts: accounts.size,
    priced: rows.filter((r) => r.currentPrice !== null).length,
    breached: rows.filter((r) => r.defenseState === 'breached').length,
    near: rows.filter((r) => r.defenseState === 'near').length,
    safe: rows.filter((r) => r.defenseState === 'safe').length,
    stale: rows.filter((r) => r.defenseState === 'stale').length,
    unknown: rows.filter((r) => r.defenseState === 'unknown').length,
    unset: rows.filter((r) => r.defenseState === 'unset').length,
    noDefense: rows.filter((r) => r.gaps.includes('no_defense')).length,
    noPrice: rows.filter((r) => r.gaps.includes('no_price')).length,
    noCost: rows.filter((r) => r.gaps.includes('no_cost')).length,
    worstByPnl,
    bestByPnl,
    allProfitable: withPnl.length > 0 && withPnl.every((r) => (r.pnlPct as number) > 0),
    conflictingSymbols,
    rows,
  }
}
