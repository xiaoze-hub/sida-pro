// @vitest-environment jsdom
/**
 * 持仓风险检查 · 渲染回归（2026-10-29）。
 *
 * 钉三件事：
 *  ① 缺现价的持仓**必须**以「无现价」成态出现（不能消失、不能显示成安全）；
 *  ② 未设防线显示「未设」，不显示任何系统折算的默认防线；
 *  ③ 只在持仓页（`viewTab==='positions'`）且有持仓时挂载，且不自己发请求。
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

function portfolioWith(positions: unknown[]): PortfolioSummary {
  return {
    accounts: [
      {
        id: 1,
        name: '主账户',
        available_funds: 1000,
        total_market_value: 3529,
        total_cost: 3652,
        total_pnl: -123,
        total_pnl_pct: -3.37,
        total_daily_pnl: -22,
        daily_pnl_period: 'today',
        daily_pnl_label: '当日盈亏',
        daily_pnl_date: '2026-10-29',
        total_assets: 4529,
        positions: positions as PortfolioSummary['accounts'][number]['positions'],
      },
    ],
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
      pos({ symbol: '600111', name: '北方稀土', defense_price: 33.96, current_price: 33.0 }),
      pos({ symbol: '000858', name: '五粮液', defense_price: 100, current_price: 101.5, cost_price: 90 }),
      pos({ symbol: '600519', name: '贵州茅台', defense_price: 1500, current_price: null, pnl_pct: null }),
      pos({ symbol: '600000', name: '浦发银行', defense_price: null }),
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
      pos({ symbol: '600111', defense_price: 33.96, current_price: 33.0 }),
      pos({ symbol: '600519', defense_price: 1500, current_price: null, pnl_pct: null }),
    ])
    render(<PositionRiskTable report={report} />)

    expect(screen.getByTestId('position-risk-row-600111').textContent).toContain('-2.83%')
    expect(screen.getByTestId('position-risk-row-600519').textContent).toContain('--')
    expect(screen.getByTestId('position-risk-row-600519').textContent).toContain('无行情')
  })

  it('行情时点用后端口径（上一交易日带日期），不写「刚刚」', () => {
    mockCtx([])
    const report = buildPositionRiskReport([
      pos({ daily_pnl_period: 'previous_trading_day', quote_date: '2026-10-28' }),
    ])
    render(<PositionRiskTable report={report} />)
    expect(screen.getByTestId('position-risk-row-600111').textContent).toContain('上一交易日 2026-10-28')
    expect(screen.getByTestId('position-risk-table').textContent).not.toContain('刚刚')
  })
})

describe('PositionRiskSection', () => {
  it('持仓页 + 有持仓 → 渲染计数与只读尾注', () => {
    mockCtx([
      pos({ symbol: '600111', defense_price: 33.96, current_price: 33.0 }),
      pos({ symbol: '600519', defense_price: null }),
    ])
    render(<PositionRiskSection />)

    const section = screen.getByTestId('position-risk-section')
    expect(section.textContent).toContain('持仓风险检查')
    expect(section.textContent).toContain('已破位')
    expect(section.textContent).toContain('未设防线')
    expect(section.textContent).toContain('不构成投资建议')
    // 未设防线要显式说明「不替用户折算纪律位」
    expect(section.textContent).toContain('系统默认值仅作参考')
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
})
