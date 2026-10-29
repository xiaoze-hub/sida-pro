// @vitest-environment jsdom
/**
 * 持仓风险检查 · 渲染回归（2026-10-29）。
 *
 * 钉这些事：
 *  ① 缺现价的持仓**必须**以「无现价」成态出现（不能消失、不能显示成安全）；
 *  ② 未设防线显示「未设」，不显示任何系统折算的默认防线；
 *  ③ 只在持仓页（`viewTab==='positions'`）且有持仓时挂载，且不自己发请求；
 *  ④ 评审补充：同标的多账户**分行**显示各自防线（一个破位一个安全），不许合并成一行；
 *  ⑤ 评审补充：旧报价显示真实日期 + 「行情滞后」，不许出现「已破位」；
 *  ⑥ 评审补充：全盈利时不说「浮亏最重」；缺口（缺价/未设防线）各自计数；
 *  ⑦ 评审补充：港美持仓标出 HKD / USD，不裸数字冒充人民币；
 *  ⑧ 评审补充：与下方持仓表的分工写清楚（不重复堆价格/成本/盈亏列）。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const ctx = vi.hoisted(() => ({
  useStocks: vi.fn(),
}))

vi.mock('@/pages/stocks/context', () => ({ useStocks: ctx.useStocks }))

import {
  PositionRiskSection,
  PositionRiskTable,
} from '@/pages/stocks/PositionRiskSection'
import { buildPositionRiskReport } from '@/lib/position-risk'
import type { PortfolioSummary } from '@/pages/stocks/shared'

afterEach(() => {
  cleanup()
  ctx.useStocks.mockReset()
})

const pos = (over: Record<string, unknown> = {}) => ({
  id: 1,
  stock_id: 1,
  symbol: '600111',
  name: '北方稀土',
  market: 'CN',
  cost_price: 36.52,
  quantity: 100,
  invested_amount: 3652,
  trading_style: 'swing',
  current_price: 35.29,
  current_price_cny: 35.29,
  change_pct: -0.62,
  market_value: 3529,
  market_value_cny: 3529,
  pnl: -123,
  pnl_pct: -3.37,
  daily_pnl: -22,
  daily_pnl_pct: -0.62,
  daily_pnl_period: 'today',
  quote_time: '2026-10-29 15:00:00',
  quote_date: '2026-10-29',
  exchange_rate: null,
  defense_price: 33.96,
  ...over,
})

function account(id: number, name: string, positions: unknown[]) {
  return {
    id,
    name,
    available_funds: 1000,
    total_market_value: 3529,
    total_cost: 3652,
    total_pnl: -123,
    total_pnl_pct: -3.37,
    total_daily_pnl: -22,
    daily_pnl_period: 'today' as const,
    daily_pnl_label: '当日盈亏',
    daily_pnl_date: '2026-10-29',
    total_assets: 4529,
    positions: positions as PortfolioSummary['accounts'][number]['positions'],
  }
}

function portfolioWith(positions: unknown[], accounts?: ReturnType<typeof account>[]): PortfolioSummary {
  return {
    accounts: accounts ?? [account(1, '主账户', positions)],
    total: {
      total_market_value: 3529,
      total_cost: 3652,
      total_pnl: -123,
      total_pnl_pct: -3.37,
      total_daily_pnl: -22,
      daily_pnl_period: 'today',
      daily_pnl_label: '当日盈亏',
      daily_pnl_date: '2026-10-29',
      available_funds: 1000,
      total_assets: 4529,
    },
  }
}

function mockCtx(positions: unknown[], viewTab = 'positions') {
  ctx.useStocks.mockReturnValue({
    portfolio: portfolioWith(positions),
    viewTab,
  })
}

describe('PositionRiskTable', () => {
  it('破位 / 逼近 / 未设防线 / 无现价 四种状态各自可见，缺现价不隐瞒', () => {
    mockCtx([])
    const report = buildPositionRiskReport([
      pos({ id: 1, symbol: '600111', name: '北方稀土', defense_price: 33.96, current_price: 33.0 }),
      pos({ id: 2, symbol: '000858', name: '五粮液', defense_price: 100, current_price: 101.5, cost_price: 90 }),
      pos({ id: 3, symbol: '600519', name: '贵州茅台', defense_price: 1500, current_price: null, pnl_pct: null }),
      pos({ id: 4, symbol: '600000', name: '浦发银行', defense_price: null }),
    ])
    render(<PositionRiskTable report={report} />)

    expect(screen.getByTestId('position-risk-row-600111').dataset.state).toBe('breached')
    expect(screen.getByTestId('position-risk-row-000858').dataset.state).toBe('near')
    expect(screen.getByTestId('position-risk-row-600519').dataset.state).toBe('unknown')
    expect(screen.getByTestId('position-risk-row-600000').dataset.state).toBe('unset')

    expect(screen.getAllByText('已破位').length).toBe(1)
    expect(screen.getByText('无现价')).toBeTruthy()
    expect(screen.getByText('未设防线')).toBeTruthy()
    // 未设防线时防线列必须是「未设」，不允许出现任何折算出来的价格
    const unsetRow = screen.getByTestId('position-risk-row-600000')
    expect(unsetRow.textContent).toContain('未设')
  })

  it('破位行的距防线为负百分比；缺现价行的距离是 --（不是 +0.00%）', () => {
    mockCtx([])
    const report = buildPositionRiskReport([
      pos({ id: 1, symbol: '600111', defense_price: 33.96, current_price: 33.0 }),
      pos({ id: 2, symbol: '600519', defense_price: 1500, current_price: null, pnl_pct: null }),
    ])
    render(<PositionRiskTable report={report} />)

    expect(screen.getByTestId('position-risk-row-600111').textContent).toContain('-2.83%')
    expect(screen.getByTestId('position-risk-row-600519').textContent).toContain('--')
    expect(screen.getByTestId('position-risk-row-600519').textContent).toContain('无行情')
  })

  it('陈旧报价：显示真实行情日期与「行情滞后」，不写「已破位」也不写「刚刚」', () => {
    mockCtx([])
    const report = buildPositionRiskReport([
      pos({
        id: 1,
        daily_pnl_period: 'previous_trading_day',
        quote_date: '2026-10-28',
        quote_time: '2026-10-28 15:00:00',
        defense_price: 34,
        current_price: 33,
      }),
    ])
    render(<PositionRiskTable report={report} />)
    const row = screen.getByTestId('position-risk-row-600111')
    expect(row.dataset.state).toBe('stale')
    expect(row.textContent).toContain('行情滞后')
    expect(row.textContent).toContain('2026-10-28')
    expect(row.textContent).not.toContain('已破位')
    expect(screen.getByTestId('position-risk-table').textContent).not.toContain('刚刚')
  })

  it('同标的多账户：两行各自账户与防线，不合并成一个状态', () => {
    mockCtx([])
    const report = buildPositionRiskReport([
      pos({ id: 11, account_id: 1, account_name: '主账户', defense_price: 34, current_price: 33 }),
      pos({ id: 12, account_id: 2, account_name: '备用账户', defense_price: 25, current_price: 33 }),
    ])
    render(<PositionRiskTable report={report} />)
    const table = screen.getByTestId('position-risk-table')
    expect(table.querySelectorAll('tbody tr').length).toBe(2)
    expect(table.textContent).toContain('主账户')
    expect(table.textContent).toContain('备用账户')
    expect(table.textContent).toContain('34')
    expect(table.textContent).toContain('25')
    expect(screen.getAllByText('已破位').length).toBe(1)
  })

  it('港/美持仓在现价与防线列标出币种', () => {
    mockCtx([])
    const report = buildPositionRiskReport([
      pos({ id: 1, symbol: '00700', name: '腾讯控股', market: 'HK', current_price: 380, defense_price: 360 }),
      pos({ id: 2, symbol: 'AAPL', name: '苹果', market: 'US', current_price: 180, defense_price: 170 }),
    ])
    render(<PositionRiskTable report={report} />)
    expect(screen.getByTestId('position-risk-row-00700').textContent).toContain('HK$380')
    expect(screen.getByTestId('position-risk-row-AAPL').textContent).toContain('US$180')
  })

  it('不重复堆价格/成本/盈亏：表里不出现「成本」「浮亏」列（那两列在下方持仓表）', () => {
    mockCtx([])
    const report = buildPositionRiskReport([pos()])
    render(<PositionRiskTable report={report} />)
    const head = screen.getByTestId('position-risk-table').querySelector('thead')?.textContent ?? ''
    expect(head).not.toContain('成本')
    expect(head).not.toContain('浮亏')
    expect(head).toContain('防线')
    expect(head).toContain('行情')
  })
})

describe('PositionRiskSection', () => {
  it('持仓页 + 有持仓 → 渲染计数、分工说明与只读尾注', () => {
    mockCtx([
      pos({ id: 1, symbol: '600111', defense_price: 33.96, current_price: 33.0 }),
      pos({ id: 2, symbol: '600519', defense_price: null }),
    ])
    render(<PositionRiskSection />)

    const section = screen.getByTestId('position-risk-section')
    expect(section.textContent).toContain('持仓风险检查')
    expect(section.textContent).toContain('已破位')
    expect(section.textContent).toContain('未设防线')
    expect(section.textContent).toContain('不构成投资建议')
    // 未设防线要显式说明「不替用户折算纪律位」
    expect(section.textContent).toContain('系统默认值仅作参考')
    // 与下方持仓表的分工写清楚，不重复堆砌
    expect(section.textContent).toContain('以下方持仓表为准')
  })

  it('非持仓页签 → 不渲染（不占自选页的可视区）', () => {
    mockCtx([pos()], 'watchlist')
    render(<PositionRiskSection />)
    expect(screen.queryByTestId('position-risk-section')).toBeNull()
  })

  it('无持仓 → 不渲染空壳', () => {
    mockCtx([])
    render(<PositionRiskSection />)
    expect(screen.queryByTestId('position-risk-section')).toBeNull()
  })

  it('一只都没有现价时，明确写「不参与判定」，不说「全部安全」', () => {
    mockCtx([pos({ symbol: '600111', current_price: null, pnl_pct: null })])
    render(<PositionRiskSection />)
    const text = screen.getByTestId('position-risk-section').textContent ?? ''
    expect(text).toContain('不参与')
    expect(text).not.toContain('全部安全')
    expect(text).toContain('有现价 0/1 只')
  })

  it('同标的多账户防线冲突：计数按账户算，并明说按账户分行、不做合并', () => {
    ctx.useStocks.mockReturnValue({
      portfolio: portfolioWith([], [
        account(1, '主账户', [pos({ id: 11, defense_price: 34, current_price: 33 })]),
        account(2, '备用账户', [pos({ id: 12, defense_price: 25, current_price: 33 })]),
      ]),
      viewTab: 'positions',
    })
    render(<PositionRiskSection />)
    const text = screen.getByTestId('position-risk-section').textContent ?? ''
    expect(text).toContain('已破位')
    // 1 破位 + 0 逼近 + 1 安全 共 2 个账户行，不是「1 只标的」
    expect(text).toContain('有现价 2/2 只')
    expect(text).toContain('按账户')
  })

  it('全盈利时不说「浮亏最重」，改说最强的一只', () => {
    mockCtx([
      pos({ id: 1, symbol: '600111', name: '北方稀土', pnl_pct: 4.2, defense_price: 30 }),
      pos({ id: 2, symbol: '601318', name: '中国平安', pnl_pct: 9.8, defense_price: 40 }),
    ])
    render(<PositionRiskSection />)
    const text = screen.getByTestId('position-risk-section').textContent ?? ''
    expect(text).not.toContain('浮亏最重')
    expect(text).toContain('浮盈')
    expect(text).toContain('中国平安')
  })

  it('无现价 + 未设防线：两个缺口计数各自体现，互不吞掉', () => {
    mockCtx([
      pos({ id: 1, symbol: '600111', current_price: null, defense_price: null, pnl_pct: null }),
    ])
    render(<PositionRiskSection />)
    const section = screen.getByTestId('position-risk-section')
    const metrics = section.querySelector('[data-testid="position-risk-metrics"]')?.textContent ?? ''
    expect(metrics).toContain('未设防线')
    expect(metrics).toContain('无现价')
    // 两个数字都必须是 1：分别取「未设防线 1」与「无现价 1」
    const unset = section.querySelector('[data-testid="metric-no-defense"]')?.textContent ?? ''
    const noPrice = section.querySelector('[data-testid="metric-no-price"]')?.textContent ?? ''
    expect(unset).toContain('1')
    expect(noPrice).toContain('1')
  })

  it('行情滞后单独成态并提示未计入破位', () => {
    mockCtx([
      pos({
        id: 1,
        symbol: '600111',
        defense_price: 34,
        current_price: 33,
        daily_pnl_period: 'previous_trading_day',
        quote_date: '2026-10-28',
        quote_time: '2026-10-28 15:00:00',
      }),
    ])
    render(<PositionRiskSection />)
    const text = screen.getByTestId('position-risk-section').textContent ?? ''
    expect(text).toContain('行情滞后')
    expect(text).toContain('未计入破位')
  })
})
