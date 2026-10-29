// @vitest-environment jsdom
// 持仓页金额口径 (2026-09-14 走查缺陷): 「可用资金 / 总资产 / 总市值」是**存量**读数,
// 旧代码走 formatMoney(= safeMoney) 渲染出 `+4.50万`, 会被读成"涨了"; 而总市值为 0 时
// 既没号也不带 + ⇒ 同一行内自相矛盾。涨跌/盈亏必须继续带符号(红涨绿跌要方向)。
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ ctx: {} as Record<string, unknown> }))

vi.mock('@/pages/stocks/context', () => ({
  useStocks: () => mocks.ctx,
  StocksContext: { Provider: ({ children }: { children: unknown }) => children },
}))

import { PortfolioSummarySection } from '@/pages/stocks/PortfolioSummarySection'
import { safeMoney } from '@/lib/format'

/** 组装组件依赖的 useStocks() 返回值(只填该组件用到的字段) */
function ctxWith(total: Record<string, unknown>, over: Record<string, unknown> = {}) {
  return {
    loadError: null,
    setLoadError: vi.fn(),
    portfolio: { total, accounts: [] },
    portfolioLoading: false,
    load: vi.fn(),
    loadPortfolio: vi.fn(),
    positionRatio: null,
    portfolioMarketStatusLabel: '',
    // 与真实 context 同源: useStocksActions 里的 formatMoney === safeMoney(带符号)
    formatMoney: (v: unknown) => safeMoney(v),
    ...over,
  }
}

describe('PortfolioSummarySection 持仓页金额符号', () => {
  afterEach(() => cleanup())
  beforeEach(() => {
    mocks.ctx = {}
  })

  it('存量读数不带 +: 可用资金/总资产/总市值都是"有多少"', () => {
    mocks.ctx = ctxWith({
      total_market_value: 0,
      total_pnl: -30000,
      total_pnl_pct: -2.5,
      total_daily_pnl: -1200,
      available_funds: 45000,
      total_assets: 1234567,
    })
    render(<PortfolioSummarySection />)

    // 可用资金 45000 元 → 4.50万(旧行为: +4.50万)
    expect(screen.getByText('4.50万')).toBeTruthy()
    expect(screen.queryByText('+4.50万')).toBeNull()
    // 总资产 1234567 元 → 123.46万(旧行为: +123.46万)
    expect(screen.getByText('123.46万')).toBeTruthy()
    expect(screen.queryByText('+123.46万')).toBeNull()
    // 总市值 0 → 0(不带号; 任何位置都不该出现 "+0")
    expect(screen.getByText('0')).toBeTruthy()
    expect(screen.queryByText('+0')).toBeNull()
  })

  it('涨跌/盈亏保留带符号口径(负号不丢)', () => {
    mocks.ctx = ctxWith({
      total_market_value: 0,
      total_pnl: -30000,
      total_pnl_pct: -2.5,
      total_daily_pnl: -12000,
      available_funds: 45000,
      total_assets: 1234567,
    })
    render(<PortfolioSummarySection />)

    expect(screen.getByText('-3.00万')).toBeTruthy()
    expect(screen.getByText('-1.20万')).toBeTruthy()
    // 百分比在括号 span 里 → 用正则匹配整段文本 "(-2.50%)"
    expect(screen.getByText(/\(-2\.50%\)/)).toBeTruthy()
  })

  it('盈亏为正时仍带 + (符号能力没被削弱)', () => {
    mocks.ctx = ctxWith({
      total_market_value: 200000,
      total_pnl: 10000,
      total_pnl_pct: 5.0,
      total_daily_pnl: 0,
      available_funds: -5000,
      total_assets: 195000,
    })
    render(<PortfolioSummarySection />)

    expect(screen.getByText('+1.00万')).toBeTruthy()
    expect(screen.getByText(/\(\+5\.00%\)/)).toBeTruthy()
    // 旧代码 `pnl >= 0 ? '+' : ''` + formatMoney(本身带 +) = "++1.00万"
    expect(screen.queryByText('++1.00万')).toBeNull()
    // 存量: 200000 → 20.00万 不带 +; 负数存量(可用为负)保留负号, 不吞符号
    expect(screen.getByText('20.00万')).toBeTruthy()
    expect(screen.queryByText('+20.00万')).toBeNull()
    expect(screen.getByText('-5000')).toBeTruthy()
  })
})

/**
 * 2026-10-29 缺行情修复的前端侧：
 * 后端一只都没价时给的是 None（未知），**不是 0，更不是 -100%**。UI 必须把 null 当"未知"
 * 渲染成 `--`，不许出现 `0`/`+0.00%`/绿色上涨色，也不许拿 null 做减法算出假百分比。
 */
describe('PortfolioSummarySection 未知(缺行情)不当 0', () => {
  afterEach(() => cleanup())
  beforeEach(() => {
    mocks.ctx = {}
  })

  const unknownTotal = {
    total_market_value: null,
    total_pnl: null,
    total_pnl_pct: null,
    total_daily_pnl: 0,
    available_funds: 45000,
    total_assets: null,
    total_cost: 12345,
    priced_cost: 0,
    unpriced_cost: 12345,
    quotes_coverage: {
      requested: true,
      positions_total: 2,
      positions_priced: 0,
      positions_missing: 2,
      cost_coverage_pct: 0,
      pricing_basis: 'priced_positions_only',
      complete: false,
    },
  }

  it('市值/盈亏/总资产为 null → 全部显示 --，不显示 0 / -100%', () => {
    mocks.ctx = ctxWith(unknownTotal)
    render(<PortfolioSummarySection />)
    const text = document.body.textContent ?? ''
    expect(text).not.toContain('-100')
    expect(text).not.toContain('+0.00%')
    expect(text).toContain('--')
    // 三个未知读数各给一个 --
    expect(screen.getAllByText('--').length).toBeGreaterThanOrEqual(3)
  })

  it('盈亏未知时不给涨跌色（不许把 null 当 0 读成上涨）', () => {
    mocks.ctx = ctxWith(unknownTotal)
    render(<PortfolioSummarySection />)
    expect(document.querySelectorAll('.text-stock-up').length).toBe(0)
    expect(document.querySelectorAll('.text-stock-down').length).toBe(0)
  })

  it('部分持仓缺行情 → 显式标注覆盖度，不让合计冒充全部持仓', () => {
    mocks.ctx = ctxWith({
      ...unknownTotal,
      total_market_value: 5000,
      total_pnl: 1000,
      total_pnl_pct: 25,
      total_assets: 50000,
      priced_cost: 4000,
      unpriced_cost: 6000,
      quotes_coverage: {
        ...unknownTotal.quotes_coverage,
        positions_priced: 1,
        positions_missing: 1,
        cost_coverage_pct: 40,
      },
    })
    render(<PortfolioSummarySection />)
    const text = document.body.textContent ?? ''
    expect(text).toContain('缺行情')
    expect(text).toContain('1/2')
    expect(text).toContain('40')
  })

  it('全覆盖 → 不显示缺行情提示（避免每次都说一句无用的话）', () => {
    mocks.ctx = ctxWith({
      total_market_value: 5000,
      total_pnl: 1000,
      total_pnl_pct: 25,
      total_daily_pnl: 0,
      available_funds: 45000,
      total_assets: 50000,
      total_cost: 4000,
      priced_cost: 4000,
      unpriced_cost: 0,
      quotes_coverage: {
        requested: true,
        positions_total: 1,
        positions_priced: 1,
        positions_missing: 0,
        cost_coverage_pct: 100,
        pricing_basis: 'all_positions',
        complete: true,
      },
    })
    render(<PortfolioSummarySection />)
    expect(document.body.textContent ?? '').not.toContain('缺行情')
  })

  /**
   * 三次复审：部分覆盖时的**总资产**不是「已定价市值 + 资金」。
   * 后端契约（380bdabd）给 total_assets=null + total_assets_priced_subset；
   * 前端必须照做：总资产 `--`、子集单独且明确标注，仓位比例也不给数字。
   */
  it('部分覆盖 → 总资产 --，只显示明确标注的「已定价子集」，表头也不出现假总额', () => {
    mocks.ctx = ctxWith({
      total_market_value: 5000,
      total_pnl: 1000,
      total_pnl_pct: 25,
      total_daily_pnl: null,
      available_funds: 45000,
      total_cost: 10000,
      priced_cost: 4000,
      unpriced_cost: 6000,
      total_assets: null,
      total_assets_complete: false,
      total_assets_basis: 'priced_positions_only',
      total_assets_priced_subset: 50000,
      quotes_coverage: {
        requested: true,
        positions_total: 2,
        positions_priced: 1,
        positions_missing: 1,
        cost_coverage_pct: 40,
        pricing_basis: 'priced_positions_only',
        complete: false,
      },
      daily_pnl_coverage: {
        requested: true,
        positions_total: 2,
        positions_observed: 0,
        complete: false,
      },
    })
    render(<PortfolioSummarySection />)
    const text = document.body.textContent ?? ''
    // 总资产必须是"未知"，并且子集口径写明「已定价子集」
    expect(text).toContain('已定价子集')
    expect(text).not.toMatch(/总资产\s*50,?000/)
    // 当日盈亏未知：不许当 0，也不许显示 +0.00%
    expect(text).toContain('当日盈亏未知')
    expect(text).not.toContain('+0.00%')
    // 仓位比例：总资产未知 ⇒ 不给数字
    expect(text).toContain('比例不给数字')
  })

  /**
   * 安全修复 8db9088b：available_funds = null（现金未登记 = 未知）。
   * 未知现金不许渲染成 0、不许算进总资产、仓位比例也不能当完整。
   */
  it('现金未知 → 可用资金写「未登记（未知）」，总资产不给数字且说明原因', () => {
    mocks.ctx = ctxWith({
      total_market_value: 5000,
      total_pnl: 1000,
      total_pnl_pct: 25,
      total_daily_pnl: 120,
      available_funds: null,
      available_funds_known: false,
      total_cost: 4000,
      priced_cost: 4000,
      unpriced_cost: 0,
      total_assets: null,
      total_assets_complete: false,
      total_assets_basis: 'available_funds_unknown',
      total_assets_priced_subset: null,
      quotes_coverage: {
        requested: true,
        positions_total: 1,
        positions_priced: 1,
        positions_missing: 0,
        cost_coverage_pct: 100,
        pricing_basis: 'all_positions',
        complete: true,
      },
      daily_pnl_coverage: {
        requested: true,
        positions_total: 1,
        positions_observed: 1,
        complete: true,
      },
    })
    render(<PortfolioSummarySection />)
    const text = document.body.textContent ?? ''
    expect(text).toContain('未登记（未知）')
    expect(text).toContain('可用资金未登记')
    // 未知现金不许显示成 0 / 0.00
    expect(screen.queryByText('0')).toBeNull()
    // 比例也不给数字
    expect(text).toContain('比例也不给')
  })

  it('当日盈亏部分覆盖 → 明确写「只统计 x/y 条持仓」', () => {
    mocks.ctx = ctxWith({
      total_market_value: 5000,
      total_pnl: 1000,
      total_pnl_pct: 25,
      total_daily_pnl: 120,
      available_funds: 45000,
      total_assets: 50000,
      total_assets_complete: true,
      total_assets_basis: 'all_positions',
      total_cost: 4000,
      priced_cost: 4000,
      unpriced_cost: 0,
      quotes_coverage: {
        requested: true,
        positions_total: 2,
        positions_priced: 2,
        positions_missing: 0,
        cost_coverage_pct: 100,
        pricing_basis: 'all_positions',
        complete: true,
      },
      daily_pnl_coverage: {
        requested: true,
        positions_total: 2,
        positions_observed: 1,
        complete: false,
      },
    })
    render(<PortfolioSummarySection />)
    const text = document.body.textContent ?? ''
    expect(text).toContain('当日盈亏只统计 1/2 条持仓')
  })
})
