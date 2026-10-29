// @vitest-environment node
/**
 * 持仓页 · 缺行情合并口径（2026-10-29 前端侧回归）。
 *
 * 背景：后端 a2454306 已把 `/portfolio/summary` 的未定价持仓改成 None + `quotes_coverage`
 * （分子分母同篮子、一只都没价时给 None 而不是 -100%）。但持仓页走的是
 * `?include_quotes=false` + 本地行情（WS/轮询）重算，**真正的定价在
 * `shared.ts::mergePortfolioQuotes` 里**，而它仍然：
 *   1. 把每一只的成本无条件累进分母（`accCost += cost`），市值只在有价时累加
 *      ⇒ 缺行情的持仓被当成"跌到 0"，全缺时 total_pnl_pct = -100%（凭空亏损）；
 *   2. `pnl_pct` 在成本 <= 0 时回退成 `0`（后端已改成 None）；
 *   3. 没把覆盖度（已定价 / 缺失 / 成本覆盖率）暴露出来，UI 无法说"部分持仓缺行情"。
 *
 * 这份测试要求客户端与后端契约**逐字段对齐**：
 *   - 未定价持仓：current_price / market_value / market_value_cny / pnl / pnl_pct 全 None，
 *     `priced=false`，`quote_status` 说明原因（missing / invalid_price），raw 值保留；
 *   - 账户/总汇总：市值与盈亏只在**已定价子集**上算，全缺 → None（未知），空账户 → 0；
 *   - 每层都给 `quotes_coverage`（与后端同结构），`pricing_basis` 区分全量与子集。
 */
import { describe, expect, it } from 'vitest'

import { computePositionRatio, mergePortfolioQuotes } from '@/pages/stocks/shared'
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
  quote_time: '2026-10-29 15:00:00',
  quote_date: '2026-10-29',
  daily_pnl_period: 'today',
  ...over,
})

/** include_quotes=false 的响应：后端不计价，持仓字段全 None（真正的口径来自客户端行情）。 */
function portfolio(accounts: { id: number; name: string; positions: unknown[] }[]): PortfolioSummary {
  return {
    accounts: accounts.map((acc) => ({
      id: acc.id,
      name: acc.name,
      available_funds: 1000,
      total_market_value: 0,
      total_cost: 0,
      total_pnl: 0,
      total_pnl_pct: 0,
      total_daily_pnl: 0,
      daily_pnl_period: 'unknown',
      daily_pnl_label: '当日盈亏',
      daily_pnl_date: null,
      total_assets: 1000,
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
      available_funds: 1000,
      total_assets: 1000,
    },
  }
}

describe('mergePortfolioQuotes · 未定价持仓不进分子分母', () => {
  it('一只都没有行情：汇总为「未知」(null)，不是 0，也不是 -100%', () => {
    const merged = mergePortfolioQuotes(
      portfolio([{ id: 1, name: '主账户', positions: [pos()] }]),
      {},
    )!
    const total = merged.total
    expect(total.total_cost).toBe(10000) // 成本是已知事实，照实给
    expect(total.total_market_value).toBeNull()
    expect(total.total_pnl).toBeNull()
    expect(total.total_pnl_pct).toBeNull()
    expect(total.total_assets).toBeNull()
    expect(total.priced_cost).toBe(0)
    expect(total.unpriced_cost).toBe(10000)
    expect(total.quotes_coverage).toMatchObject({
      requested: true,
      positions_total: 1,
      positions_priced: 0,
      positions_missing: 1,
      cost_coverage_pct: 0,
      pricing_basis: 'priced_positions_only',
      complete: false,
    })
    // 未定价持仓本身也不许出现 0 市值 / 0 盈亏
    const p = merged.accounts[0].positions[0]
    expect(p.current_price).toBeNull()
    expect(p.market_value).toBeNull()
    expect(p.market_value_cny).toBeNull()
    expect(p.pnl).toBeNull()
    expect(p.pnl_pct).toBeNull()
    expect(p.priced).toBe(false)
    expect(p.quote_status).toBe('missing')
  })

  it('部分覆盖：盈亏用「已定价子集」的成本做分母，缺行情的不摊薄也不算亏', () => {
    const merged = mergePortfolioQuotes(
      portfolio([
        {
          id: 1,
          name: '主账户',
          positions: [
            pos({ id: 1, symbol: '600111', cost_price: 40, quantity: 100 }), // 成本 4000
            pos({ id: 2, symbol: '600519', cost_price: 60, quantity: 100 }), // 成本 6000，无行情
          ],
        },
      ]),
      { 'CN:600111': quote({ current_price: 50, change_pct: 0 }) }, // 市值 5000
    )!
    const total = merged.total
    expect(total.total_market_value).toBe(5000)
    expect(total.total_pnl).toBe(1000) // 5000 - 4000（不是 5000 - 10000 = -5000）
    expect(total.total_pnl_pct).toBe(25) // 1000 / 4000
    expect(total.total_cost).toBe(10000)
    expect(total.priced_cost).toBe(4000)
    expect(total.unpriced_cost).toBe(6000)
    expect(total.quotes_coverage).toMatchObject({
      positions_total: 2,
      positions_priced: 1,
      positions_missing: 1,
      cost_coverage_pct: 40,
      pricing_basis: 'priced_positions_only',
      complete: false,
    })
    // 账户层同篮子
    const acc = merged.accounts[0]
    expect(acc.total_pnl).toBe(1000)
    expect(acc.total_pnl_pct).toBe(25)
    expect(acc.quotes_coverage!.positions_missing).toBe(1)
  })

  it('全覆盖：与修复前逐字段一致，覆盖度标 complete', () => {
    const merged = mergePortfolioQuotes(
      portfolio([
        {
          id: 1,
          name: '主账户',
          positions: [
            pos({ id: 1, symbol: '600111', cost_price: 40, quantity: 100 }),
            pos({ id: 2, symbol: '600519', cost_price: 60, quantity: 100 }),
          ],
        },
      ]),
      {
        'CN:600111': quote({ current_price: 50 }),
        'CN:600519': quote({ current_price: 50 }),
      },
    )!
    const total = merged.total
    expect(total.total_market_value).toBe(10000)
    expect(total.total_pnl).toBe(0)
    expect(total.total_pnl_pct).toBe(0)
    expect(total.total_assets).toBe(11000) // 市值 + 可用资金
    expect(total.quotes_coverage).toMatchObject({
      positions_total: 2,
      positions_priced: 2,
      positions_missing: 0,
      cost_coverage_pct: 100,
      pricing_basis: 'all_positions',
      complete: true,
    })
  })

  it('坏报价（0 / 负数 / 非数值）按「无效」处理，不算 0 元市值', () => {
    const merged = mergePortfolioQuotes(
      portfolio([{ id: 1, name: '主账户', positions: [pos({ symbol: '600111' })] }]),
      {
        'CN:600111': quote({ current_price: 0 }),
        'CN:600519': quote({ current_price: -3 }),
      },
    )!
    const p = merged.accounts[0].positions[0]
    expect(p.current_price).toBeNull()
    expect(p.priced).toBe(false)
    expect(p.quote_status).toBe('invalid_price')
    expect(p.raw_current_price).toBe(0)
    expect(merged.total.total_pnl).toBeNull()
  })

  it('空账户：0 只持仓是真 0，不是「未知」；可用资金照常进总资产', () => {
    const merged = mergePortfolioQuotes(portfolio([{ id: 1, name: '空账户', positions: [] }]), {})!
    const total = merged.total
    expect(total.total_market_value).toBe(0)
    expect(total.total_pnl).toBe(0)
    expect(total.total_pnl_pct).toBe(0)
    expect(total.total_assets).toBe(1000)
    expect(total.quotes_coverage!.positions_total).toBe(0)
  })

  it('后端 include_quotes=true 已给现价时仍可用持仓价兜底（行情未到时不清空）', () => {
    const merged = mergePortfolioQuotes(
      portfolio([
        { id: 1, name: '主账户', positions: [pos({ symbol: '600111', current_price: 120 })] },
      ]),
      {},
    )!
    expect(merged.accounts[0].positions[0].current_price).toBe(120)
    expect(merged.total.total_market_value).toBe(12000)
    expect(merged.total.quotes_coverage!.positions_priced).toBe(1)
  })

  it('当日盈亏只统计已定价持仓，缺行情的不记 0 涨跌', () => {
    const merged = mergePortfolioQuotes(
      portfolio([
        {
          id: 1,
          name: '主账户',
          positions: [
            pos({ id: 1, symbol: '600111', cost_price: 100, quantity: 100 }),
            pos({ id: 2, symbol: '600519', cost_price: 100, quantity: 100 }),
          ],
        },
      ]),
      { 'CN:600111': quote({ current_price: 110, change_pct: 10 }) },
    )!
    const priced = merged.accounts[0].positions.find((p) => p.symbol === '600111')!
    const unpriced = merged.accounts[0].positions.find((p) => p.symbol === '600519')!
    expect(priced.daily_pnl).not.toBeNull()
    expect(unpriced.daily_pnl).toBeNull()
    expect(unpriced.daily_pnl_pct).toBeNull()
    // 只有 1 只参与，覆盖度也只有一个观察值 ⇒ 时点明确（today），不是 mixed
    expect(merged.total.daily_pnl_period).toBe('today')
  })

  it('不计价的空响应不炸：portfolio 为 null 时原样返回 null', () => {
    expect(mergePortfolioQuotes(null, {})).toBeNull()
  })
})

describe('computePositionRatio · 未知不当 0%', () => {
  it('总市值未知（null）→ 仓位比例未知，不是 0.0%', () => {
    const ratio = computePositionRatio({
      total_market_value: null,
      total_assets: null,
      available_funds: 1000,
    })
    expect(ratio).toEqual({ mv: null, assets: null, pct: null })
  })

  it('已知时照常算比例', () => {
    const ratio = computePositionRatio({
      total_market_value: 5000,
      total_assets: 10000,
      available_funds: 5000,
    })
    expect(ratio).toEqual({ mv: 5000, assets: 10000, pct: 50 })
  })

  it('总资产为 0（空仓无现金）→ 比例未知，不写 0%', () => {
    const ratio = computePositionRatio({
      total_market_value: 0,
      total_assets: 0,
      available_funds: 0,
    })
    expect(ratio.pct).toBeNull()
  })
})
