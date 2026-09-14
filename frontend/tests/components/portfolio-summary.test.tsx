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
