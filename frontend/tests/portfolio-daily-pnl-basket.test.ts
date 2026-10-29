// @vitest-environment node
/**
 * 当日盈亏 · 百分比与金额必须同一个篮子（2026-10-29 独立复审 F4）。
 *
 * `total_daily_pnl` 只累加**有昨收/涨跌幅**的持仓（缺昨收的算不出来，不按 0 计）；
 * 但 `PortfolioSummarySection` 反推"上日市值"时用的是 `total_market_value - total_daily_pnl`，
 * 而 `total_market_value` 是**全部已定价持仓**的市值。两个篮子不同 ⇒ 百分比错：
 *   持仓 A：市值 11000、当日 +1000（+10%）；持仓 B：市值 5000、无昨收（算不出当日盈亏）。
 *   旧口径：1000 / (16000 - 1000) = 6.67% —— 既不是 A 的 +10%，也不是组合的真实涨跌。
 *   新口径：1000 / (11000 - 1000) = 10.0%，与 A 的涨跌幅一致（同一篮子）。
 *
 * 所以合并时必须把「参与当日盈亏的持仓市值」显式给出来（`daily_pnl_basket_value`），
 * 让消费方自己别去拿全价篮子反推；拿不到时百分比就是未知，不退回全价篮子。
 */
import { describe, expect, it } from 'vitest'

import { mergePortfolioQuotes } from '@/pages/stocks/shared'
import type { PortfolioSummary, QuoteSnapshot } from '@/pages/stocks/shared'

const pos = (over: Record<string, unknown> = {}) => ({
  id: 1,
  stock_id: 1,
  symbol: '600111',
  name: '北方稀土',
  market: 'CN',
  cost_price: 100,
  quantity: 100,
  invested_amount: 10000,
  trading_style: 'swing',
  current_price: null,
  current_price_cny: null,
  change_pct: null,
  market_value: null,
  market_value_cny: null,
  pnl: null,
  pnl_pct: null,
  daily_pnl: null,
  daily_pnl_pct: null,
  daily_pnl_period: 'unknown' as const,
  quote_time: null,
  quote_date: null,
  exchange_rate: null,
  defense_price: null,
  ...over,
})

const quote = (over: Partial<QuoteSnapshot> = {}): QuoteSnapshot => ({
  current_price: 110,
  change_pct: 10,
  quote_time: '2026-10-29 14:00:00',
  quote_date: '2026-10-29',
  daily_pnl_period: 'today',
  ...over,
})

function portfolio(
  accounts: { id: number; name: string; positions: unknown[] }[],
  rates?: { HKD_CNY: number },
): PortfolioSummary {
  return {
    accounts: accounts.map((acc) => ({
      id: acc.id,
      name: acc.name,
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
      positions: acc.positions,
    })) as unknown as PortfolioSummary['accounts'],
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
    exchange_rates: rates,
  }
}

describe('mergePortfolioQuotes · 当日盈亏篮子', () => {
  it('只有部分持仓有当日盈亏时，篮子 = 这些持仓的市值（不是全价市值）', () => {
    const merged = mergePortfolioQuotes(
      portfolio([
        {
          id: 1,
          name: '主账户',
          positions: [
            pos({ id: 1, symbol: 'A', cost_price: 100, quantity: 100 }), // 110 × 100 = 11000，当日 +1000
            pos({ id: 2, symbol: 'B', cost_price: 50, quantity: 100 }), // 50 × 100 = 5000，无昨收
          ],
        },
      ]),
      {
        'CN:A': quote({ current_price: 110, change_pct: 10 }),
        'CN:B': quote({ current_price: 50, change_pct: null }),
      },
    )!

    const total = merged.total
    expect(total.total_market_value).toBe(16000)
    expect(total.total_daily_pnl).toBe(1000)
    // 关键：篮子只含观测到的那一条
    expect(total.daily_pnl_basket_value).toBe(11000)
    // 同一篮子反推的上日市值 = 10000 ⇒ 与 A 自己的 +10% 完全一致
    const pct = (total.total_daily_pnl as number) / ((total.daily_pnl_basket_value as number) - (total.total_daily_pnl as number)) * 100
    expect(pct).toBeCloseTo(10, 6)
    // 若错用全价篮子则是 6.67%（这行是这条回归的"错答"）
    const wrong = (total.total_daily_pnl as number) / ((total.total_market_value as number) - (total.total_daily_pnl as number)) * 100
    expect(wrong).not.toBeCloseTo(pct, 2)
  })

  it('账户级同样给篮子（与账户级 total_daily_pnl 同源）', () => {
    const merged = mergePortfolioQuotes(
      portfolio([
        {
          id: 1,
          name: '主账户',
          positions: [
            pos({ id: 1, symbol: 'A', quantity: 100 }),
            pos({ id: 2, symbol: 'B', cost_price: 50, quantity: 100 }),
          ],
        },
      ]),
      {
        'CN:A': quote({ current_price: 110, change_pct: 10 }),
        'CN:B': quote({ current_price: 50, change_pct: null }),
      },
    )!
    const acc = merged.accounts[0]
    expect(acc.total_daily_pnl).toBe(1000)
    expect(acc.daily_pnl_basket_value).toBe(11000)
    expect(acc.daily_pnl_coverage).toMatchObject({ positions_observed: 1, positions_total: 2, complete: false })
  })

  it('一只都没观测到 → 金额与篮子都是 null（未知，不是 0）', () => {
    const merged = mergePortfolioQuotes(
      portfolio([{ id: 1, name: '主账户', positions: [pos({ symbol: 'B', cost_price: 50 })] }]),
      { 'CN:B': quote({ current_price: 50, change_pct: null }) },
    )!
    expect(merged.total.total_daily_pnl).toBeNull()
    expect(merged.total.daily_pnl_basket_value).toBeNull()
  })

  it('港股持仓的篮子换算成人民币（与 total_daily_pnl 同币种，比例才成立）', () => {
    const merged = mergePortfolioQuotes(
      portfolio(
        [{ id: 1, name: '港股账户', positions: [pos({ symbol: '00700', market: 'HK', cost_price: 300, quantity: 100 })] }],
        { HKD_CNY: 0.9 },
      ),
      { 'HK:00700': quote({ current_price: 330, change_pct: 10 }) },
    )!
    // 330 × 100 × 0.9 = 29700 (CNY)，当日 +2700
    expect(merged.total.daily_pnl_basket_value).toBe(29700)
    expect(merged.total.total_daily_pnl).toBe(2700)
    const pct = (2700 / (29700 - 2700)) * 100
    expect(pct).toBeCloseTo(10, 6)
  })

  it('空账户：当日盈亏是真 0，篮子也是 0（比例因此未知，不是 0%）', () => {
    const merged = mergePortfolioQuotes(portfolio([{ id: 1, name: '空账户', positions: [] }]), {})!
    expect(merged.total.total_daily_pnl).toBe(0)
    expect(merged.total.daily_pnl_basket_value).toBe(0)
  })
})
