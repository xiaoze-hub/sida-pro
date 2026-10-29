// @vitest-environment node
/**
 * B2（2026-10-29 独立复审）· 无时刻的 WS 涨跌幅不许混进「已知今日」篮子。
 *
 * 链路：WS 帧只有 `{price, change_pct, prev_close}`，没有行情源报时。换价后
 * `daily_pnl_period` 被诚实降级为 `unknown`（F3 已修），但合并层仍把这条 `unknown`
 * 的涨跌幅当"当日"算进 `total_daily_pnl`，并用它进 `summarizeDailyPnlPeriod` 的观测集：
 *   - `periods = {today, unknown}` ⇒ `daily_pnl_period='mixed'` ⇒ 标签写「最近交易日盈亏」
 *     （把"不知道是不是今天"讲成了"上个交易日"，还暗示合计是同一交易日的）；
 *   - 合计里混着一条**日期不明**的涨跌幅，却顶着「今日盈亏」的分母。
 *
 * 现在的口径：期间不可证的涨跌幅**不进篮子**（当日盈亏 = null，涨跌幅也不陈列 —— 它可能
 * 是上一交易日的涨跌），并且必须老实说清覆盖缺口（`daily_pnl_coverage.positions_observed`
 * 不把它算作已观测）。`mixed` 只留给真正的「今天 + 上一交易日」混排。
 */
import { describe, expect, it } from 'vitest'

import { dailyPnlDisplayLabel, mergePortfolioQuotes, round2, summarizeDailyPnlPeriod } from '@/pages/stocks/shared'
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

const todayQuote: QuoteSnapshot = {
  current_price: 102,
  change_pct: 2,
  quote_time: '2026-10-29 09:31:00',
  quote_date: '2026-10-29',
  daily_pnl_period: 'today',
}

/** WS 换价后的形态：价格有了、涨跌幅有了，但时刻/日期/期间全部未知。 */
const unknownPeriodQuote: QuoteSnapshot = {
  current_price: 110,
  change_pct: 10,
  quote_time: null,
  quote_date: null,
  daily_pnl_period: 'unknown',
}

describe('mergePortfolioQuotes · 期间未知的涨跌幅不进当日篮子', () => {
  const merged = mergePortfolioQuotes(
    portfolio([
      position({ id: 1, symbol: '600111' }),
      position({ id: 2, symbol: '600222', name: '另一只' }),
    ]),
    { 'CN:600111': todayQuote, 'CN:600222': unknownPeriodQuote },
  )!

  it('unknown 期间那条的当日盈亏/涨跌幅都是未知（不是 0，也不是"当日"）', () => {
    const row = merged.accounts[0].positions.find((p) => p.symbol === '600222')!
    expect(row.current_price).toBe(110)
    expect(row.daily_pnl).toBeNull()
    expect(row.daily_pnl_pct).toBeNull()
    expect(row.change_pct).toBeNull()
    expect(row.daily_pnl_period).toBe('unknown')
  })

  it('合计只按有期间证明的那条算，且标签仍是「今日盈亏」而不是「最近交易日」', () => {
    const acc = merged.accounts[0]
    // 只有 600111：prev = 102/1.02 = 100，daily = (102-100)*100 = 200
    expect(acc.total_daily_pnl).toBe(round2(200))
    expect(acc.daily_pnl_basket_value).toBe(round2(102 * 100))
    expect(acc.daily_pnl_period).toBe('today')
    expect(acc.daily_pnl_label).toBe('今日盈亏')
    expect(dailyPnlDisplayLabel(acc, true)).toBe('今日')
    expect(merged.total.daily_pnl_period).toBe('today')
    expect(merged.total.total_daily_pnl).toBe(round2(200))
  })

  it('覆盖缺口如实计数：期间未知的那条算"未观测"，合计不许假装全覆盖', () => {
    const cov = merged.accounts[0].daily_pnl_coverage!
    expect(cov.positions_total).toBe(2)
    expect(cov.positions_observed).toBe(1)
    expect(cov.complete).toBe(false)
  })

  it('全部期间未知 → 当日盈亏未知（null），标签不写「最近交易日」', () => {
    const onlyUnknown = mergePortfolioQuotes(portfolio([position()]), {
      'CN:600111': unknownPeriodQuote,
    })!
    const acc = onlyUnknown.accounts[0]
    expect(acc.total_daily_pnl).toBeNull()
    expect(acc.daily_pnl_period).toBe('unknown')
    expect(acc.daily_pnl_coverage?.positions_observed).toBe(0)
    expect(dailyPnlDisplayLabel(acc, true)).not.toBe('最近交易日')
  })

  it('真·混排（今天 + 上一交易日）仍然叫「最近交易日」（不给未知背这个名）', () => {
    const meta = summarizeDailyPnlPeriod([
      { period: 'today', date: '2026-10-29' },
      { period: 'previous_trading_day', date: '2026-10-28' },
      { period: 'unknown', date: null },
    ])
    expect(meta.daily_pnl_period).toBe('mixed')
    expect(meta.daily_pnl_label).toBe('最近交易日盈亏')
  })

  it('summarize 单独吃 unknown 观测时不当成"今日"', () => {
    const meta = summarizeDailyPnlPeriod([{ period: 'unknown', date: null }])
    expect(meta.daily_pnl_period).toBe('unknown')
    expect(dailyPnlDisplayLabel(meta, true)).not.toBe('今日')
  })
})
