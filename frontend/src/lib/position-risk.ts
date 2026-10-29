/**
 * 持仓风险检查（P2 持仓风险切片, 2026-10-29）—— 纯函数，无 React、无请求。
 *
 * ## 为什么要有这一层
 * 持仓页只有**组合合计**（总市值/总盈亏），逐只的「有没有纪律位、离纪律位多远、哪只
 * 压根没有现价」散在三处：`DefenseLineBanner`（只在击穿时弹一条）、`AccountsSection`
 * （表格里没有防线列）、`/portfolio/diagnostics`（只有集中度，且无前端消费者）。
 * 这一层把同一份 `/portfolio/summary` 读数折成**逐只可核对的检查清单**，供持仓页与
 * 参谋卡片共用一份判定，不再各写一套阈值。
 *
 * ## 口径（每条都对应一次真实事故）
 * 1. **破位只认用户自设防线**（2026-10-23 产品裁决 B）：`positions.defense_price` 为空
 *    ⇒ `unset`，既不是安全也不是破位。系统不许拿 `成本 × (1-8%)` 冒充用户纪律位。
 * 2. **缺失不许静默**：`current_price` 为空 ⇒ `unknown`，并从 `noPrice` 里数出来。把
 *    缺行情的持仓当「安全」等于替用户判了个没有数据支撑的结论。
 * 3. **`price === defense` 算破位**（`<=`），与 `DefenseLineBanner` 现行判定一致；
 *    「逼近」是半开区间 `(0, NEAR_LINE_PCT]`，正好 3% 仍算逼近。
 * 4. **PG DECIMAL 经 JSON 是字符串**：所有数值一律走 `safeNum`，不许 `"100" <= "99"` 的
 *    字典序比较（那会把安全判成破位）。
 * 5. **只产出观测值**：状态 / 距离 / 缺口。不产出方向、买卖、目标价 —— 结论交给人。
 * 6. **跨账户同标的合并**：同一 `market:symbol` 多账户持仓合成一行（数量相加、成本按
 *    数量加权），否则同一只票会出现两行且各自口径不同；合并后 `pnl_pct` 必须重算，
 *    不能拿某一个账户的后端值冒充合并后的浮亏。
 */

import { safeNum } from './format'

/** 距防线多近算「逼近」。与 `DefenseLineBanner` 同源（该组件已改为 import 本常量）。 */
export const NEAR_LINE_PCT = 3

export type DefenseState = 'breached' | 'near' | 'safe' | 'unset' | 'unknown'

/** 数据缺口（**不是**风险结论）：缺现价 / 缺用户防线 / 缺成本。 */
export type RiskGap = 'no_price' | 'no_defense' | 'no_cost'

export type PricePeriod = 'today' | 'previous_trading_day' | 'mixed' | 'unknown'

/** `/portfolio/summary` 持仓行的最小子集；数值允许是字符串（PG DECIMAL）。 */
export interface PositionRiskInput {
  symbol: string
  name?: string | null
  market?: string | null
  quantity?: number | string | null
  cost_price?: number | string | null
  current_price?: number | string | null
  pnl_pct?: number | string | null
  defense_price?: number | string | null
  quote_date?: string | null
  daily_pnl_period?: string | null
}

export interface PositionRiskRow {
  /** 合并键 `market:symbol`。 */
  key: string
  symbol: string
  name: string
  market: string
  costPrice: number | null
  currentPrice: number | null
  /** 后端给的浮亏百分比；合并行按加权成本重算。缺价/缺成本时为 null，不回退成 0。 */
  pnlPct: number | null
  defensePrice: number | null
  defenseState: DefenseState
  /** `(现价 - 防线) / 防线 * 100`；只有 breached/near/safe 三者非 null。 */
  distanceToDefensePct: number | null
  quoteDate: string | null
  pricePeriod: PricePeriod
  gaps: RiskGap[]
}

export interface PositionRiskReport {
  /** 去重后的持仓数（跨账户同标的算一只）。 */
  total: number
  /** 有可用现价的只数 —— 分母是它，不是 total。 */
  priced: number
  breached: number
  near: number
  noDefense: number
  noPrice: number
  /** 浮亏最重的一只（只在实际有现价的持仓里比）；没有可比对象时为 null。 */
  worstByPnl: PositionRiskRow | null
  /** 已按处置优先级排序：破位（越深越前）→ 逼近 → 缺现价 → 未设防线 → 安全。 */
  rows: PositionRiskRow[]
}

const PERIODS: ReadonlySet<string> = new Set(['today', 'previous_trading_day', 'mixed', 'unknown'])

/** 正数才有效（价格/成本/防线为 0 或负数都是"没有这个数"，不是 0）。 */
function positive(v: unknown): number | null {
  const n = safeNum(v)
  return n !== null && n > 0 ? n : null
}

const STATE_RANK: Record<DefenseState, number> = {
  breached: 0,
  near: 1,
  unknown: 2,
  unset: 3,
  safe: 4,
}

/**
 * 单只持仓的防线判定 —— **全站唯一实现**。
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

interface MergedPosition {
  symbol: string
  name: string
  market: string
  quantity: number
  costBasis: number | null
  price: number | null
  pnlPct: number | null
  defense: number | null
  quoteDate: string | null
  period: PricePeriod
  /** 后端 pnl_pct 只在**单账户**行可信；合并行必须重算。 */
  merged: boolean
}

function mergePositions(positions: PositionRiskInput[]): MergedPosition[] {
  const out = new Map<string, MergedPosition>()
  for (const p of positions) {
    const symbol = String(p.symbol ?? '').trim()
    if (!symbol) continue
    const market = String(p.market ?? 'CN').trim().toUpperCase() || 'CN'
    const key = `${market}:${symbol}`
    const qty = safeNum(p.quantity)
    const cost = positive(p.cost_price)
    const price = positive(p.current_price)
    const defense = positive(p.defense_price)
    const rawPeriod = String(p.daily_pnl_period ?? '')
    const period: PricePeriod = (PERIODS.has(rawPeriod) ? rawPeriod : 'unknown') as PricePeriod

    const prev = out.get(key)
    if (!prev) {
      out.set(key, {
        symbol,
        name: String(p.name ?? symbol).trim() || symbol,
        market,
        quantity: qty !== null && qty > 0 ? qty : 0,
        costBasis: cost !== null && qty !== null && qty > 0 ? cost * qty : null,
        price,
        pnlPct: safeNum(p.pnl_pct),
        defense,
        quoteDate: p.quote_date ?? null,
        period,
        merged: false,
      })
      continue
    }

    prev.merged = true
    prev.quantity += qty !== null && qty > 0 ? qty : 0
    if (cost !== null && qty !== null && qty > 0) {
      prev.costBasis = (prev.costBasis ?? 0) + cost * qty
    }
    // 同一标的多个账户：任一账户有价即可用（报价是同一只票的同一时刻）
    if (price !== null) {
      prev.price = price
      prev.quoteDate = p.quote_date ?? prev.quoteDate
      prev.period = period
    }
    if (defense !== null) prev.defense = defense
  }
  return [...out.values()]
}

/**
 * 把 `/portfolio/summary` 的持仓行折成风险检查报告。
 *
 * 纯函数：不请求、不读时间（时点用后端已判好的 `daily_pnl_period` + `quote_date`，
 * 前端自己判「今天」会在非交易时段把上一交易日的读数标成当日）。
 */
export function buildPositionRiskReport(positions: PositionRiskInput[] | null | undefined): PositionRiskReport {
  const merged = mergePositions(Array.isArray(positions) ? positions : [])

  const rows: PositionRiskRow[] = merged.map((p) => {
    const costPrice = p.costBasis !== null && p.quantity > 0 ? p.costBasis / p.quantity : null
    const { state, distancePct } = evaluateDefenseLine(p.price, p.defense)

    const gaps: RiskGap[] = []
    if (p.price === null) gaps.push('no_price')
    if (p.defense === null) gaps.push('no_defense')
    if (costPrice === null) gaps.push('no_cost')

    let pnlPct: number | null = null
    if (p.price !== null && costPrice !== null) {
      // 单账户且后端给了浮亏百分比 → 用后端口径（它含汇率换算，前端重算会差一点）；
      // 合并行 / 后端缺值 → 按加权成本自行推算，并允许它随后显示为自算值。
      pnlPct = !p.merged && p.pnlPct !== null ? p.pnlPct : ((p.price - costPrice) / costPrice) * 100
    }

    return {
      key: `${p.market}:${p.symbol}`,
      symbol: p.symbol,
      name: p.name,
      market: p.market,
      costPrice,
      currentPrice: p.price,
      pnlPct,
      defensePrice: p.defense,
      defenseState: state,
      distanceToDefensePct: distancePct,
      quoteDate: p.quoteDate,
      pricePeriod: p.period,
      gaps,
    }
  })

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
    return a.symbol.localeCompare(b.symbol)
  })

  const pricedRows = rows.filter((r) => r.currentPrice !== null)
  let worstByPnl: PositionRiskRow | null = null
  for (const r of pricedRows) {
    if (r.pnlPct === null) continue
    if (worstByPnl === null || (worstByPnl.pnlPct ?? 0) > r.pnlPct) worstByPnl = r
  }

  return {
    total: rows.length,
    priced: pricedRows.length,
    breached: rows.filter((r) => r.defenseState === 'breached').length,
    near: rows.filter((r) => r.defenseState === 'near').length,
    noDefense: rows.filter((r) => r.defenseState === 'unset').length,
    noPrice: rows.filter((r) => r.defenseState === 'unknown').length,
    worstByPnl,
    rows,
  }
}
