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

  it('【复审】部分覆盖时**不给总资产**：子集口径单独给数，不冒充总额', () => {
    const merged = mergePortfolioQuotes(
      portfolio([
        {
          id: 1,
          name: '主账户',
          positions: [
            pos({ id: 1, symbol: '600111', cost_price: 40, quantity: 100 }), // 有价
            pos({ id: 2, symbol: '600519', cost_price: 60, quantity: 100 }), // 缺价
          ],
        },
      ]),
      { 'CN:600111': quote({ current_price: 50, change_pct: 0 }) },
    )!
    const total = merged.total
    // 「已定价 5000 + 可用 1000」不是总资产（缺价那只的市值没进来）：总资产必须未知。
    expect(total.total_assets).toBeNull()
    expect(total.total_assets_complete).toBe(false)
    expect(total.total_assets_basis).toBe('priced_positions_only')
    expect(total.total_assets_priced_subset).toBe(6000)
    // 仓位比例跟着未知（旧实现会拿子集市值 / 子集总资产算出 83.3%，把缺价当空仓）
    expect(computePositionRatio(total).pct).toBeNull()

    const acc = merged.accounts[0]
    expect(acc.total_assets).toBeNull()
    expect(acc.total_assets_complete).toBe(false)
    expect(acc.total_assets_basis).toBe('priced_positions_only')
    expect(acc.total_assets_priced_subset).toBe(6000)
  })

  it('【复审】全部有价才给总资产，并标 all_positions / complete', () => {
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
    expect(merged.total.total_assets).toBe(11000)
    expect(merged.total.total_assets_complete).toBe(true)
    expect(merged.total.total_assets_basis).toBe('all_positions')
    expect(computePositionRatio(merged.total).pct).toBeCloseTo(90.909, 2)
  })

  it('【复审】有持仓但一只都没价：总资产口径标 no_priced_positions，也不给子集数', () => {
    const merged = mergePortfolioQuotes(
      portfolio([{ id: 1, name: '主账户', positions: [pos()] }]),
      {},
    )!
    expect(merged.total.total_assets).toBeNull()
    expect(merged.total.total_assets_basis).toBe('no_priced_positions')
    expect(merged.total.total_assets_priced_subset).toBeNull()
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

describe('daily_pnl_coverage · 缺涨跌幅/昨收不许静默少算', () => {
  it('部分持仓没有涨跌幅：合计只覆盖已观测部分，覆盖度显式给出', () => {
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
      {
        // 有价但没有涨跌幅（行情源没给昨收）⇒ 当日盈亏算不出，不许记 0
        'CN:600111': quote({ current_price: 110, change_pct: null }),
        'CN:600519': quote({ current_price: 105, change_pct: 5 }),
      },
    )!
    const total = merged.total
    expect(total.total_daily_pnl).not.toBeNull()
    expect(total.daily_pnl_coverage).toEqual({
      requested: true,
      positions_total: 2,
      positions_observed: 1,
      complete: false,
    })
    // 未被观测到的那只：daily_pnl 是 null（不是 0）
    const noChange = merged.accounts[0].positions.find((p) => p.symbol === '600111')!
    expect(noChange.daily_pnl).toBeNull()
    expect(noChange.daily_pnl_pct).toBeNull()
  })

  it('一只都观测不到：当日盈亏为 null（未知），不是 0', () => {
    const merged = mergePortfolioQuotes(
      portfolio([{ id: 1, name: '主账户', positions: [pos({ symbol: '600111' })] }]),
      { 'CN:600111': quote({ current_price: 110, change_pct: null }) },
    )!
    expect(merged.total.total_daily_pnl).toBeNull()
    expect(merged.total.daily_pnl_coverage).toMatchObject({
      positions_total: 1,
      positions_observed: 0,
      complete: false,
    })
    expect(merged.accounts[0].total_daily_pnl).toBeNull()
  })

  it('全覆盖：complete=true，合计与逐只相加一致', () => {
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
      {
        'CN:600111': quote({ current_price: 110, change_pct: 10 }),
        'CN:600519': quote({ current_price: 100, change_pct: 0 }),
      },
    )!
    expect(merged.total.daily_pnl_coverage).toEqual({
      requested: true,
      positions_total: 2,
      positions_observed: 2,
      complete: true,
    })
    const sum = merged.accounts[0].positions.reduce((acc, p) => acc + (p.daily_pnl ?? 0), 0)
    expect(merged.total.total_daily_pnl).toBeCloseTo(sum, 2)
  })

  it('空账户：0 只持仓 ⇒ 0 当日盈亏 + no_positions（不是"缺行情"）', () => {
    const merged = mergePortfolioQuotes(portfolio([{ id: 1, name: '空账户', positions: [] }]), {})!
    expect(merged.total.total_daily_pnl).toBe(0)
    expect(merged.total.daily_pnl_coverage).toEqual({
      requested: true,
      positions_total: 0,
      positions_observed: 0,
      complete: true,
    })
    expect(merged.total.quotes_coverage).toMatchObject({
      positions_total: 0,
      pricing_basis: 'no_positions',
      complete: true,
    })
  })
})

/**
 * 安全修复 8db9088b 契约：`available_funds` 可能是 **null = 现金未知**（从未登记），
 * 不是 0。后端给 `available_funds_known=false` + `total_assets_basis=available_funds_unknown`
 * + `total_assets=None`。前端 merge 若把 null 当 0，会把「不知道有多少现金」算成
 * 「一分钱没有」，总资产/仓位比例全都虚低且看起来完全正常 —— 这比报错更危险。
 */
describe('available_funds 未知（null）不被当成 0', () => {
  const withFunds = (
    funds: number | null,
    positions: unknown[],
  ): PortfolioSummary => {
    const p = portfolio([{ id: 1, name: '主账户', positions }])
    return {
      ...p,
      accounts: p.accounts.map((a) => ({ ...a, available_funds: funds })) as PortfolioSummary['accounts'],
      total: { ...p.total, available_funds: funds },
    }
  }

  it('全部有价但现金未知 → 总资产仍为 null，basis=available_funds_unknown，子集也不给', () => {
    const merged = mergePortfolioQuotes(
      withFunds(null, [pos({ id: 1, symbol: '600111', cost_price: 40, quantity: 100 })]),
      { 'CN:600111': quote({ current_price: 50 }) },
    )!
    const total = merged.total
    expect(total.available_funds).toBeNull()
    expect(total.available_funds_known).toBe(false)
    expect(total.total_assets).toBeNull()
    expect(total.total_assets_complete).toBe(false)
    expect(total.total_assets_basis).toBe('available_funds_unknown')
    // 现金未知时，"已定价市值 + 未知现金" 也不是可展示的子集口径
    expect(total.total_assets_priced_subset).toBeNull()
    // 仓位比例跟着未知（不许拿 0 现金算出 100%）
    expect(computePositionRatio(total).pct).toBeNull()

    const acc = merged.accounts[0]
    expect(acc.available_funds).toBeNull()
    expect(acc.available_funds_known).toBe(false)
    expect(acc.total_assets).toBeNull()
    expect(acc.total_assets_basis).toBe('available_funds_unknown')
  })

  it('现金未知 + 空账户 → 总资产未知（不是 0 元）', () => {
    const merged = mergePortfolioQuotes(withFunds(null, []), {})!
    expect(merged.total.total_assets).toBeNull()
    expect(merged.total.total_assets_basis).toBe('available_funds_unknown')
    expect(merged.total.total_assets_complete).toBe(false)
  })

  it('一个账户现金未知 → 汇总现金与总资产都未知（不把已知账户的现金冒充合计）', () => {
    const base = portfolio([
      { id: 1, name: '主账户', positions: [pos({ id: 1, symbol: '600111', cost_price: 40, quantity: 100 })] },
      { id: 2, name: '老账户', positions: [pos({ id: 2, symbol: '600519', cost_price: 60, quantity: 100 })] },
    ])
    const mixed: PortfolioSummary = {
      ...base,
      accounts: base.accounts.map((a, i) => ({
        ...a,
        available_funds: i === 0 ? 1000 : null,
      })) as PortfolioSummary['accounts'],
      total: { ...base.total, available_funds: null },
    }
    const merged = mergePortfolioQuotes(mixed, {
      'CN:600111': quote({ current_price: 50 }),
      'CN:600519': quote({ current_price: 50 }),
    })!
    expect(merged.total.available_funds).toBeNull()
    expect(merged.total.available_funds_known).toBe(false)
    expect(merged.total.total_assets).toBeNull()
    expect(merged.accounts[0].available_funds).toBe(1000)
    expect(merged.accounts[1].available_funds).toBeNull()
  })

  it('现金已知（含 0）→ 口径不变：总资产 = 全部市值 + 现金，complete=true', () => {
    const merged = mergePortfolioQuotes(
      withFunds(0, [pos({ id: 1, symbol: '600111', cost_price: 40, quantity: 100 })]),
      { 'CN:600111': quote({ current_price: 50 }) },
    )!
    expect(merged.total.available_funds).toBe(0)
    expect(merged.total.available_funds_known).toBe(true)
    expect(merged.total.total_assets).toBe(5000)
    expect(merged.total.total_assets_basis).toBe('all_positions')
    expect(merged.total.total_assets_complete).toBe(true)
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
