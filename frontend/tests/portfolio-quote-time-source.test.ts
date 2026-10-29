// @vitest-environment node
/**
 * 行情合并 · 价格与时刻必须来自**同一个源**（2026-10-29 独立复审 F3 的合并侧）。
 *
 * `mergePortfolioQuotes` 旧写法逐字段回退：
 *   `quote_time = quote?.quote_time ?? pos.quote_time`
 *   `current_price = quotePrice ?? heldPrice`
 * 于是出现"价格取自本地行情、时刻取自后端持仓行"的错配：
 *   - WS 推来的 14:00 新价（无报价时刻）配上后端持仓行里 09:31 的旧时刻；
 *   - 下游按 09:31 判时效 ⇒ 要么把新价当"滞后 4.5 小时"（误报陈旧），
 *     要么在时钟也冻结时把它当"刚发生的"（冒充实时）；
 *   - `daily_pnl_period` 同样串源，当日盈亏落到错的交易日上。
 *
 * 现在的规则：**先定价格来源，时刻/日期/期间只能跟同一个来源走**。
 * 本地行情给了价就用本地行情的时刻（没有就是 null = 未知），不许用后端那份顶替。
 */
import { describe, expect, it } from 'vitest'

import { mergePortfolioQuotes } from '@/pages/stocks/shared'
import type { PortfolioSummary, QuoteSnapshot } from '@/pages/stocks/shared'

const position = (over: Record<string, unknown> = {}) => ({
  id: 1,
  stock_id: 1,
  symbol: '600111',
  name: '北方稀土',
  market: 'CN',
  cost_price: 100,
  quantity: 100,
  invested_amount: 10000,
  trading_style: 'swing',
  // include_quotes=false 时后端仍会带上"库内最后一笔价 + 它的时刻"
  current_price: 100,
  current_price_cny: 100,
  change_pct: 0,
  market_value: 10000,
  market_value_cny: 10000,
  pnl: 0,
  pnl_pct: 0,
  daily_pnl: 0,
  daily_pnl_pct: 0,
  daily_pnl_period: 'today' as const,
  quote_time: '2026-10-29 09:31:00',
  quote_date: '2026-10-29',
  exchange_rate: null,
  defense_price: null,
  ...over,
})

function portfolio(positions: unknown[]): PortfolioSummary {
  return {
    accounts: [
      {
        id: 1,
        name: '主账户',
        available_funds: 0,
        total_market_value: 0,
        total_cost: 0,
        total_pnl: 0,
        total_pnl_pct: 0,
        total_daily_pnl: 0,
        daily_pnl_period: 'unknown',
        daily_pnl_label: '当日盈亏',
        daily_pnl_date: null,
        total_assets: 0,
        positions,
      },
    ] as unknown as PortfolioSummary['accounts'],
    total: {
      total_market_value: 0,
      total_cost: 0,
      total_pnl: 0,
      total_pnl_pct: 0,
      total_daily_pnl: 0,
      daily_pnl_period: 'unknown',
      daily_pnl_label: '当日盈亏',
      daily_pnl_date: null,
      available_funds: 0,
      total_assets: 0,
    },
  }
}

const localQuote = (over: Partial<QuoteSnapshot> = {}): QuoteSnapshot => ({
  current_price: 110,
  change_pct: 5,
  quote_time: '2026-10-29 14:00:00',
  quote_date: '2026-10-29',
  daily_pnl_period: 'today',
  ...over,
})

describe('mergePortfolioQuotes · 价格与时刻同源', () => {
  it('本地行情有价但没有报价时刻（WS 推价）→ 时刻/日期/期间一律未知，不借后端那份', () => {
    const merged = mergePortfolioQuotes(portfolio([position()]), {
      'CN:600111': localQuote({ quote_time: null, quote_date: null, daily_pnl_period: 'unknown' }),
    })!
    const p = merged.accounts[0].positions[0]
    expect(p.current_price).toBe(110)
    // 关键：不许 110 配上后端的 09:31
    expect(p.quote_time).toBeNull()
    expect(p.quote_date).toBeNull()
    expect(p.daily_pnl_period).toBe('unknown')
  })

  it('本地行情带时刻 → 用本地行情的时刻/日期/期间（不留后端旧值）', () => {
    const merged = mergePortfolioQuotes(portfolio([position()]), { 'CN:600111': localQuote() })!
    const p = merged.accounts[0].positions[0]
    expect(p.current_price).toBe(110)
    expect(p.quote_time).toBe('2026-10-29 14:00:00')
    expect(p.quote_date).toBe('2026-10-29')
    expect(p.daily_pnl_period).toBe('today')
  })

  it('完全没有本地行情 → 整组沿用后端持仓行（价与时刻成对，不拆开用）', () => {
    const merged = mergePortfolioQuotes(portfolio([position()]), {})!
    const p = merged.accounts[0].positions[0]
    expect(p.current_price).toBe(100)
    expect(p.quote_time).toBe('2026-10-29 09:31:00')
    expect(p.daily_pnl_period).toBe('today')
  })

  it('本地行情给了坏价（<=0）→ 回落到后端价时，时刻也随之回落（不可交叉）', () => {
    const merged = mergePortfolioQuotes(portfolio([position()]), {
      'CN:600111': localQuote({ current_price: 0, quote_time: null, quote_date: null, daily_pnl_period: 'unknown' }),
    })!
    const p = merged.accounts[0].positions[0]
    // 价来自后端，时刻也来自后端；不许"后端价 + 本地空时刻"这种半串
    expect(p.current_price).toBe(100)
    expect(p.quote_time).toBe('2026-10-29 09:31:00')
  })
})
