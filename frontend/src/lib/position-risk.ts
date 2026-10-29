/**
 * 持仓风险检查（P2 持仓风险切片, 2026-10-29）—— 纯函数，无 React、无请求、无时钟。
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
 * 7. **全盈利不许叫「浮亏最重」**：`worstByPnl` 只是「按盈亏最弱的一只」（可能是正收益），
 *    `allProfitable` 交给 UI 换措辞。
 * 8. **缺口独立计数**：既无现价又未设防线时，`noPrice` 与 `noDefense` 各算一次 ——
 *    旧实现用状态桶计数，后一个缺口被前一个吃掉。
 * 9. **币种按市场**：HK→HKD、US→USD、CN→CNY；一张表里混排三种币却不标币种，
 *    读起来就是「腾讯 380 比茅台 1500 便宜」。
 * 10. **缺成本不稀释**：旧合并把「无成本账户的数量」也算进加权分母，摊薄了另一账户的成本。
 *     现在每行独立，缺失直接 `no_cost` + `pnlPct=null`。
 *
 * ## 2026-10-29 三次评审：行情时效（本条最容易被写错）
 * 11. **「backend says today」不等于「这条报价是刚发生的」**：`daily_pnl_period` 只回答
 *     「报价日期是不是市场当地的今天」。09:31 的报价在 14:00 依然是 `today`，拿它判破位
 *     等于用五个半小时前的价位说「你现在跌破了纪律位」。所以时效判定要**两层**：
 *       - 日期层：后端按**市场时区**判好的 `daily_pnl_period`（前端不复刻交易日历）；
 *       - 时刻层：报价自身的 `quote_time`（行情源的报时，服务器下行）与**服务器给出的
 *         市场墙钟**（`/stocks/markets/status` 的 `local_time` + `local_date`）之差。
 *         两者同一时区、**同一天**，才谈得上「刚发生的」。
 * 12. **拿不到可核对的时间就不许冒充实时**：没有 `quote_time`、没有服务器市场时钟、
 *     报价晚于市场时钟（跨日/时钟异常）、**报价日期或市场日期缺一个**（`HH:mm` 无从证明
 *     跨日）⇒ `unknown`，**不下**破位结论——宁可不判，也不编一个「行情正常」的结论。
 *     库内不读系统时钟（不调用 Date 的无参构造 / now()）、不自推交易日历。
 * 13. **阈值是推断值**：`QUOTE_FRESH_WINDOW_MINUTES` 不是交易所/后端契约，只是「超过这么多
 *     分钟就不再当实时」的经验线，UI 必须显式标注它，不许当成官方口径。
 * 14. **服务器说市场没在交易时，当日报价是「定格价」而不是「实时价」**：`/stocks/markets/status`
 *     的 `status`（trading / break / after_hours / pre_market / closed / unknown）本身就是
 *     服务器判的，不是前端猜的交易日历。非交易时段里当日最后一笔报价可以判（那是全市场
 *     看到的同一价位），但必须标成 `非交易时段 · 最近报价`，不许写「实时」。
 */

import { safeFixed, safeNum, safePrice } from './format'

/** 距防线多近算「逼近」。与 `DefenseLineBanner` 同源（该组件已改为 import 本常量）。 */
export const NEAR_LINE_PCT = 3

/**
 * 行情时效窗口（分钟）—— **推断值，不是交易所/后端契约**。
 *
 * 取 15 分钟的理由（写清楚，避免下一个人当它是官方口径）：
 *  - 持仓页行情由 `/quotes/batch` 轮询 + WS 推送刷新，轮询间隔远小于 15 分钟，
 *    所以真·实时的报价不会落到窗口外；
 *  - 它只用于**否决**「实时」这个说法，不用于放行：超窗 ⇒ 标记滞后并停判破位；
 *  - 不做「交易时段内/外」的推断（那是交易日历的活，属于服务器），只做时刻差。
 */
export const QUOTE_FRESH_WINDOW_MINUTES = 15

/**
 * 防线结论。`stale` = 有价也有防线，但报价的**时点**无法证明是当下的 ——
 * 此时**不下**破位/安全结论（行情滞后 / 时点未证实都落在这里）。
 */
export type DefenseState = 'breached' | 'near' | 'safe' | 'unset' | 'unknown' | 'stale'

/** 数据缺口（**不是**风险结论）：缺现价 / 缺用户防线 / 缺成本。 */
export type RiskGap = 'no_price' | 'no_defense' | 'no_cost'

/**
 * 行情时效：
 *  - `current`：服务器判定「交易日 + 交易中」，且报价时刻在窗口内；
 *  - `session_frozen`：服务器判定当日非交易时段（休市/盘后/午休/盘前），当日最后一笔报价；
 *    可判（全市场同一价位），但必须标「非实时」；
 *  - `stale`：已知不是当日，或当日但超出窗口；
 *  - `unknown`：**证明不了**（无报价时刻 / 无服务器市场时钟 / 时刻异常）；
 *  - `missing`：没有现价。
 */
export type QuoteFreshness = 'current' | 'session_frozen' | 'stale' | 'unknown' | 'missing'

/** 时效结论的原因，UI 据此说清「为什么不算实时」，不含糊其辞。 */
export type FreshnessReason =
  | 'ok'
  | 'period'
  | 'period_unknown'
  | 'intraday_age'
  | 'no_time'
  | 'no_clock'
  | 'clock_skew'
  | 'refresh_failed'
  | 'missing'

export type PricePeriod = 'today' | 'previous_trading_day' | 'mixed' | 'unknown'

export type Currency = 'CNY' | 'HKD' | 'USD' | 'UNKNOWN'

/**
 * 服务器给出的市场状态（`GET /stocks/markets/status` 的子集）。
 *
 * `localTime` 是后端用 `datetime.now(market_tz)` 算出来的**服务器市场墙钟**，
 * 与行情源的 `quote_time` 同时区；`status` 是后端按交易日历给出的状态。
 * 这两个字段是「时效判定」唯一允许的外部事实（前端不自己算时段/日历）。
 */
export interface MarketSessionInfo {
  /** "HH:MM"（服务器市场当地墙钟）。也可能带日期（"YYYY-MM-DD HH:MM"），带上就直接比对日期。 */
  localTime?: string | null
  /**
   * 服务器市场当地**日期**（`YYYY-MM-DD`）。`/stocks/markets/status` 自 2026-10-29
   * 起独立下发 `local_date`（该市场时区的当天）。
   *
   * 这是**日级一致性**的唯一依据：只有 `HH:mm` 时，昨天 09:31 的报价配今天 09:31 的钟
   * 会算出 0 分钟（旧算法的错判）。缺这个字段 ⇒ 时点未证实（`period_unknown`），
   * 不允许用 `HH:mm` 自证。
   */
  localDate?: string | null
  /** trading / break / after_hours / pre_market / closed / unknown。 */
  status?: string | null
}

export type MarketSessionMap = Record<string, MarketSessionInfo | undefined> | null | undefined

export interface FreshnessVerdict {
  freshness: QuoteFreshness
  reason: FreshnessReason
  /** 报价时刻距服务器市场墙钟的分钟数；算不出（跨日/缺时刻）为 null。 */
  ageMinutes: number | null
  /** 报价的市场当地墙钟 "HH:MM"。 */
  quoteLocalTime: string | null
  /** 服务器市场墙钟 "HH:MM"。 */
  marketLocalTime: string | null
  /** 服务器给出的市场状态（原样透传，供 UI 说明「非交易时段」）。 */
  marketStatus: string | null
}

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
  /** 报价的市场当地墙钟 "HH:MM"（解析不出为 null）。 */
  quoteLocalTime: string | null
  /** 服务器市场墙钟 "HH:MM"（拿不到为 null）。 */
  marketLocalTime: string | null
  /** 服务器给出的市场状态（trading / break / after_hours / …），用于区分「交易中」与「非交易时段」。 */
  marketStatus: string | null
  pricePeriod: PricePeriod
  freshness: QuoteFreshness
  /** 这是「不算实时」的具体原因，UI 必须把它说出来。 */
  staleReason: FreshnessReason
  /** 报价距服务器市场墙钟的分钟数（算不出为 null）。 */
  quoteAgeMinutes: number | null
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
  /**
   * 有价有防线但**时效未证实**（滞后 / 时点未知）的行数 —— 未计入破位/逼近。
   * 与表格「时效未证实」状态严格同数（同一 `defenseState`）。
   */
  stale: number
  /** 其中「已知不是当下」的行数（日期层或时刻层超窗）。 */
  staleKnown: number
  /** 其中「证明不了时点」的行数（缺报价时刻 / 缺服务器市场时钟 / 时刻异常）。 */
  staleUnverified: number
  /** 没有可用现价（连防线都不判）—— 与 `noPrice` 缺口同源但这里是状态桶。 */
  unknown: number
  /** 有现价但用户没设防线（不告警，当然也不判破位）。 */
  unset: number
  /** 未设防线的**行**数（与表格「你的防线=未设」严格同数）。 */
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
  /** 已按处置优先级排序：破位（越深越前）→ 逼近 → 时效未证实 → 缺现价 → 未设防线 → 安全。 */
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

/** 服务器状态里哪些算「当日非交易时段」（仍可判，但只能叫定格价）。 */
export const FROZEN_SESSION_STATUSES: ReadonlySet<string> = new Set([
  'break',
  'after_hours',
  'pre_market',
  'closed',
])

/**
 * 这个服务器市场状态是否属于「当日非交易时段」（定格价语义）。
 * 导出给告警条的**休市降频**复用 —— 时段白名单只此一份，避免两处各写一套而漂移。
 */
export function sessionIsFrozen(status: unknown): boolean {
  return FROZEN_SESSION_STATUSES.has(String(status ?? '').trim().toLowerCase())
}

/**
 * 「不会马上开盘」的休市态（2026-10-29 降频用）。
 *
 * 只用 `after_hours` / `closed`：午休（break）与盘前（pre_market）几分钟内就会恢复交易，
 * 把它们也算作空闲会让开盘后的第一次取价晚最多两分钟 —— 漏判比多取一轮更贵。
 */
export const IDLE_SESSION_STATUSES: ReadonlySet<string> = new Set(['after_hours', 'closed'])

/** 服务器市场状态是否属于「不会马上开盘」的休市态（告警条可据此降频）。 */
export function sessionIsIdle(status: unknown): boolean {
  return IDLE_SESSION_STATUSES.has(String(status ?? '').trim().toLowerCase())
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

/** 币种符号（UI 拼价格前缀用；未知市场为空串，不冒充人民币）。 */
export function currencySymbolForMarket(market: unknown): string {
  return CURRENCY_SYMBOL[currencyForMarket(market)]
}

/**
 * 价格带币种前缀。港股/美股必须标出来 —— 否则同一张表里的 380（HKD）和 1500（CNY）
 * 会被当成同一把尺子比较。
 */
export function formatPriceWithCurrency(price: unknown, market: unknown): string {
  const n = safeNum(price)
  if (n === null) return '--'
  // 价格精度走 safePrice（R6 门禁：禁裸 toFixed）
  return `${currencySymbolForMarket(market)}${safePrice(n, 4)}`
}

/**
 * 从 "YYYY-MM-DD HH:MM[:SS]" / "YYYY-MM-DDTHH:MM[:SS]±HH:MM" 里取**当地墙钟分钟数**。
 *
 * 只读字符串里的时分：行情源的 `quote_time` 是**市场当地**时间（后端按市场时区定位，
 * 见 `packages/marketdata .../tencent.py::_parse_quote_time`），
 * 服务器市场时钟 `local_time` 也是市场当地，两者同一天才可比。
 * 因此这里**不**把字符串转成绝对时刻（那要猜时区/夏令时），也不读本机时钟。
 */
export function wallClockMinutes(value: unknown): number | null {
  const text = String(value ?? '').trim()
  if (!text) return null
  const m = /(?:^|[ T])(\d{1,2}):(\d{2})(?::\d{2})?/.exec(text)
  if (!m) return null
  const h = Number(m[1])
  const min = Number(m[2])
  if (!Number.isFinite(h) || !Number.isFinite(min)) return null
  if (h < 0 || h > 23 || min < 0 || min > 59) return null
  return h * 60 + min
}

/** "HH:MM"（分钟数 → 展示用；算不出给 null）。 */
export function formatWallClockMinutes(minutes: number | null): string | null {
  if (minutes === null || !Number.isFinite(minutes)) return null
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

/**
 * 从字符串里取**日历日**（`YYYY-MM-DD`）。取不到给 null。
 *
 * 只做"有没有日期"这件事，不做时区换算/日期加减（那是服务器按市场时区算好的活）。
 * 报价日期（`quote_date`）与服务器市场墙钟（`local_time`/`localDate`）都带日期时，
 * 两者必须相等才谈得上"同一交易日"（2026-10-29 复审 B3）。
 */
export function wallClockDate(value: unknown): string | null {
  const text = String(value ?? '').trim()
  if (!text) return null
  const m = /(\d{4})-(\d{2})-(\d{2})/.exec(text)
  if (!m) return null
  const month = Number(m[2])
  const day = Number(m[3])
  if (month < 1 || month > 12 || day < 1 || day > 31) return null
  return `${m[1]}-${m[2]}-${m[3]}`
}

/**
 * 行情时效判定 —— **唯一实现**（持仓清单与告警条共用）。
 *
 * 只看两件服务器给的事实：`daily_pnl_period`（市场时区判好的日期层）与
 * `/stocks/markets/status` 的市场墙钟 + 状态（时刻层 / 时段层）。
 * 任何一层缺失 ⇒ `unknown`（证明不了，不冒充实时），绝不 fallback 成「today 就算实时」。
 */
export function freshnessFor(args: {
  price: unknown
  period?: string | null
  quoteTime?: string | null
  /**
   * 报价自身的**日历日**（`quote_date`）。缺省时退回 `quoteTime` 里的日期段。
   *
   * 2026-10-29 复审 B3：`period='today'` 只说明"取数那一刻后端判它是当日"，
   * 而 `HH:mm` 这种没有日期的钟**不能**自证跨日 —— 昨天 09:31 的价配今天 09:31 的钟，
   * 旧的算法算出差 0 分钟就判 `current`。现在带日期的服务器市场墙钟
   * （`market.localDate` / `localTime` 内含日期）与报价日期都已知时必须相等。
   */
  quoteDate?: string | null
  market?: MarketSessionInfo | null
  /**
   * 这份读数本身是否"仍然被成功刷新过"（默认 `true`）。
   *
   * 2026-10-29 独立复审 F2：告警条原来只在挂载时取一次行情，之后长期不刷新 ——
   * 价与市场墙钟**一起停在同一时刻**，年龄恒为 0，旧价被判成实时。
   * 调用方在"续期失败"时必须传 `false`：手里这份快照已经无从证明是当下的，
   * 哪怕它的 `quote_time` 看起来还很新。
   *
   * 2026-10-29 复审 B3 补充：服务器市场墙钟目前只有 `HH:MM`（无日期）时，
   * "这份快照是否还在本地有效期内"**只能由调用方保证**（`useStocksData` 的
   * `QUOTE_SNAPSHOT_TTL_MS` + 市场墙钟回绕检测）。过期 ⇒ 这里必须收到 `false`，
   * 绝不允许"钟一样 ⇒ 就是同一分钟"的自证。
   */
  verified?: boolean
}): FreshnessVerdict {
  const quoteLocalMinutes = wallClockMinutes(args.quoteTime)
  const marketLocalMinutes = wallClockMinutes(args.market?.localTime)
  const marketStatusRaw = String(args.market?.status ?? '').trim().toLowerCase()
  const quoteDate = wallClockDate(args.quoteDate) ?? wallClockDate(args.quoteTime)
  const quoteTimeDate = wallClockDate(args.quoteTime)
  const marketDate = wallClockDate(args.market?.localDate) ?? wallClockDate(args.market?.localTime)
  const verdict = (
    freshness: QuoteFreshness,
    reason: FreshnessReason,
    ageMinutes: number | null = null,
  ): FreshnessVerdict => ({
    freshness,
    reason,
    ageMinutes,
    quoteLocalTime: formatWallClockMinutes(quoteLocalMinutes),
    marketLocalTime: formatWallClockMinutes(marketLocalMinutes),
    marketStatus: marketStatusRaw || null,
  })

  if (positive(args.price) === null) return verdict('missing', 'missing')

  // ① 续期闸门：这份读数已经不再被成功刷新 ⇒ 时点无从证明（不许拿旧快照判破位）。
  if (args.verified === false) return verdict('unknown', 'refresh_failed')

  const period = String(args.period ?? '')
  // ② 日期层：后端按市场时区判好的「是不是今天」。已知非今日 ⇒ 一律滞后（隔日/隔周都算）。
  if (period === 'previous_trading_day' || period === 'mixed') {
    return verdict('stale', 'period')
  }
  // 日期层本身证明不了（WS 换价后期间未知 / 缺字段）⇒ 时点未知，不能当"历史报价"来说
  // （那会写成"历史报价（日期未知）"，等于把"不知道"讲成"旧"）。
  // 具体原因按缺哪一层给：连报价时刻都没有 ⇒ no_time；有时刻但确认不了属于当日 ⇒ period_unknown。
  if (period !== 'today') {
    return quoteLocalMinutes === null
      ? verdict('unknown', 'no_time')
      : verdict('unknown', 'period_unknown')
  }

  // ③ 时刻层：没有报价时刻 ⇒ 证明不了「刚发生」，不许当实时。
  if (quoteLocalMinutes === null) return verdict('unknown', 'no_time')
  // ③b 日期层自洽（2026-10-29 复审 B3）：报价日期与报价时刻里的日期不一致 ⇒ 内部矛盾，
  //     不猜哪一个是真的（跨日残留 / 字段串源），一律时点存疑。
  if (quoteDate !== null && quoteTimeDate !== null && quoteDate !== quoteTimeDate) {
    return verdict('unknown', 'clock_skew')
  }
  // ④ 没有服务器市场墙钟 ⇒ 无从比较，不猜本机时间。
  if (marketLocalMinutes === null) return verdict('unknown', 'no_clock')
  // ③c 日级证据两边都必须有（2026-10-29 复审 B3 收紧）：`HH:mm` **不能自证**是同一分钟。
  //     昨天 09:31 的报价配今天 09:31 的钟，算出来 age=0 —— 这正是隔日旧价冒充实时的形态。
  //     报价日期或服务器市场日期缺一个 ⇒ 时点未证实（`period_unknown`），不判当前。
  if (quoteDate === null || marketDate === null) return verdict('unknown', 'period_unknown')
  // ③d 两边都有日期 ⇒ 必须同一天；不同天就是已知陈旧（`stale/period`），不是"未知"。
  if (quoteDate !== marketDate) return verdict('stale', 'period')

  const age = marketLocalMinutes - quoteLocalMinutes
  if (age < 0) {
    // 报价晚于服务器市场时钟：跨日残留、盘中跨零点、或时钟异常。
    return verdict('unknown', 'clock_skew', age)
  }

  if (marketStatusRaw === 'trading') {
    return age <= QUOTE_FRESH_WINDOW_MINUTES
      ? verdict('current', 'ok', age)
      : verdict('stale', 'intraday_age', age)
  }

  // ⑤ 服务器说当日非交易时段（午休/盘后/休市/盘前）：当日最后一笔报价是「定格价」。
  //    这种情况下全市场看到的是同一价位，可以判，但必须标成非实时。
  if (sessionIsFrozen(marketStatusRaw)) {
    return age <= QUOTE_FRESH_WINDOW_MINUTES
      ? verdict('current', 'ok', age)
      : verdict('session_frozen', 'ok', age)
  }

  // trading/break/... 之外的 'unknown' / 缺字段 ⇒ 时段未知，同样不判。
  return verdict('unknown', 'no_clock', age)
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

/**
 * 防线结论 + 时效闸门 —— **全站唯一实现**（`DefenseLineBanner` 与持仓清单共用）。
 *
 * 报价的**时点**证明不了是当下的（`stale` / `unknown`）时不给破位/逼近/安全结论：
 * 旧报价当实时价判破位，等于拿隔日甚至隔周（或同一交易日几小时前）的价位说
 * "你现在已经跌破纪律位"。距离仍返回（可核对），但状态是 `stale`。
 */
export function resolveDefenseState(args: {
  price: unknown
  defense: unknown
  period?: string | null
  quoteTime?: string | null
  /** 报价自身的日历日（见 `freshnessFor` 的同名参数；B3 跨日判定用）。 */
  quoteDate?: string | null
  market?: MarketSessionInfo | null
  /** 读数是否仍在被成功刷新（见 `freshnessFor` 的同名参数）。 */
  verified?: boolean
}): {
  state: DefenseState
  distancePct: number | null
  freshness: QuoteFreshness
  reason: FreshnessReason
  ageMinutes: number | null
} {
  const verdict = freshnessFor({
    price: args.price,
    period: args.period,
    quoteTime: args.quoteTime,
    quoteDate: args.quoteDate,
    market: args.market,
    verified: args.verified,
  })
  const base = {
    freshness: verdict.freshness,
    reason: verdict.reason,
    ageMinutes: verdict.ageMinutes,
  }
  if (verdict.freshness === 'missing') {
    return { ...base, state: 'unknown', distancePct: null }
  }
  const geo = evaluateDefenseLine(args.price, args.defense)
  if (geo.state === 'unset') return { ...base, ...geo }
  if (verdict.freshness !== 'current' && verdict.freshness !== 'session_frozen') {
    return { state: 'stale', distancePct: geo.distancePct, ...base }
  }
  return { ...base, ...geo }
}

/**
 * 报价距今多久（分钟口径，**只用注入的时点**）。
 *
 * `now` 必须由调用方注入：库内读系统时钟会让测试不可重复，也会掩盖"没拿到时间"。
 * 无法解析 / 未来时间（跨市场时区没对齐时会出现）一律 null —— 宁可不显示，也不编一个时点。
 * 注意：持仓清单优先用服务器市场墙钟的分钟差（`row.quoteAgeMinutes`），本函数只服务
 * 「手上有绝对时点且明确知道它代表什么」的调用方。
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
  return humanizeAgeMinutes(diffMs / 60000)
}

/** 分钟数 → 「N 分钟前 / N.N 小时前 / N.N 天前」。 */
export function humanizeAgeMinutes(minutes: number | null): string | null {
  if (minutes === null || !Number.isFinite(minutes)) return null
  if (minutes < 60) return `${Math.round(minutes)} 分钟前`
  const hours = minutes / 60
  if (hours < 36) return `${safeFixed(hours, 1)} 小时前`
  return `${safeFixed(hours / 24, 1)} 天前`
}

/** 把一行持仓读数折成风险行（每账户一行，绝不跨账户合并防线/成本）。 */
export function rowFromPosition(
  p: PositionRiskInput,
  marketSessions?: MarketSessionMap,
  /**
   * 这份读数是否仍在被成功续期（2026-10-29 F2/B1）。`false` ⇒ 时效一律「未核实」，
   * 有价有防线也不判破位（手里这份快照已无从证明是当下的）。
   */
  verified?: boolean,
): PositionRiskRow {
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
  const marketSession = marketSessions?.[market] ?? null
  const { state, distancePct, freshness, reason, ageMinutes } = resolveDefenseState({
    price,
    defense,
    period: pricePeriod,
    quoteTime: p.quote_time,
    quoteDate: p.quote_date,
    market: marketSession,
    verified,
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
    quoteLocalTime: formatWallClockMinutes(wallClockMinutes(p.quote_time)),
    marketLocalTime: formatWallClockMinutes(wallClockMinutes(marketSession?.localTime)),
    marketStatus: String(marketSession?.status ?? '').trim().toLowerCase() || null,
    pricePeriod,
    freshness,
    staleReason: reason,
    quoteAgeMinutes: ageMinutes,
    sharedSymbolConflict: false,
    gaps,
  }
}

/**
 * 把 `/portfolio/summary` 的持仓行折成风险检查报告。
 *
 * 纯函数：不请求、不读时钟。行情时效用两件**服务器事实**：
 * 后端判好的 `daily_pnl_period`（日期层）+ `/stocks/markets/status` 的市场墙钟/状态
 * （时刻层）。前端自己不判「今天」也不推交易日历 —— 那两件事服务器已经做了。
 */
export function buildPositionRiskReport(
  positions: PositionRiskInput[] | null | undefined,
  options?: {
    marketSessions?: MarketSessionMap
    /** 这份读数是否仍在被成功续期（见 `rowFromPosition` 同名参数）；缺省视为 true。 */
    verified?: boolean
  },
): PositionRiskReport {
  const marketSessions = options?.marketSessions
  const verified = options?.verified
  const rows = (Array.isArray(positions) ? positions : [])
    .filter((p) => String(p.symbol ?? '').trim() !== '')
    .map((p) => rowFromPosition(p, marketSessions, verified))

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
  const staleRows = rows.filter((r) => r.defenseState === 'stale')

  return {
    total: rows.length,
    symbols: symbols.size,
    accounts: accounts.size,
    priced: rows.filter((r) => r.currentPrice !== null).length,
    breached: rows.filter((r) => r.defenseState === 'breached').length,
    near: rows.filter((r) => r.defenseState === 'near').length,
    safe: rows.filter((r) => r.defenseState === 'safe').length,
    stale: staleRows.length,
    staleKnown: staleRows.filter((r) => r.freshness === 'stale').length,
    staleUnverified: staleRows.filter((r) => r.freshness === 'unknown').length,
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
