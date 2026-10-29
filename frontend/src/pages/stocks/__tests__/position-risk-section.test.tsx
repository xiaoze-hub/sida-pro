// @vitest-environment jsdom
/**
 * 持仓风险检查 · 渲染回归（2026-10-29）。
 *
 * 钉这些事：
 *  ① 缺现价的持仓**必须**以「无现价」成态出现（不能消失、不能显示成安全）；
 *  ② 未设防线显示「未设」，不显示任何系统折算的默认防线；
 *  ③ 只在持仓页（`viewTab==='positions'`）且有持仓时挂载，且不自己发请求；
 *  ④ 同标的多账户**分行**显示各自防线（一个破位一个安全），不许合并成一行；
 *  ⑤ 旧报价显示真实日期 + 「时效未证实」，不许出现「已破位」；
 *  ⑥ 全盈利时不说「浮亏最重」；缺口（缺价/未设防线）各自计数；
 *  ⑦ 港美持仓标出 HKD / USD，不裸数字冒充人民币；
 *  ⑧ 与下方持仓表的分工写清楚（不重复堆价格/成本/盈亏列）。
 *
 * 三次评审（日内陈旧行情）：
 *  ⑨ 09:31 的报价在 14:03 **不许**显示「已破位」：状态是「时效未证实」，行情列写明
 *     「滞后 N 小时 · 报价 09:31」（不再只看「是不是今天」）；
 *  ⑩ 没有报价时刻（WS tick 只剩裸价）/ 没有服务器市场时钟 ⇒ 时点未知，同样不判破位；
 *  ⑪ 指标数字与表格行数严格同源（同一批行），单位写「行」不冒称「只」；
 *  ⑫ JSX 里不许出现 markdown 星号（`**xx**` 会原样渲染出来）。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const ctx = vi.hoisted(() => ({
  useStocks: vi.fn(),
}))

vi.mock('@/pages/stocks/context', () => ({ useStocks: ctx.useStocks }))

import { PositionRiskSection, PositionRiskTable } from '@/pages/stocks/PositionRiskSection'
import { buildPositionRiskReport } from '@/lib/position-risk'
import type { MarketSessionMap } from '@/lib/position-risk'
import type { PortfolioSummary } from '@/pages/stocks/shared'

afterEach(() => {
  cleanup()
  ctx.useStocks.mockReset()
})

/** 服务器市场状态夹具：`/stocks/markets/status` 里 CN 当天 15:03、交易中。 */
const MARKET_STATUS = [
  { code: 'CN', name: 'A股', status: 'trading', status_text: '交易中', local_time: '15:03', local_date: '2026-10-29' },
  { code: 'HK', name: '港股', status: 'trading', status_text: '交易中', local_time: '15:03', local_date: '2026-10-29' },
  { code: 'US', name: '美股', status: 'trading', status_text: '交易中', local_time: '15:03', local_date: '2026-10-29' },
]

const SESSIONS: MarketSessionMap = {
  CN: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
  HK: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
  US: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
}

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

function mockCtx(
  positions: unknown[],
  viewTab = 'positions',
  marketStatus: unknown = MARKET_STATUS,
) {
  ctx.useStocks.mockReturnValue({
    portfolio: portfolioWith(positions),
    viewTab,
    marketStatus,
  })
}

const reportOf = (
  rows: Parameters<typeof buildPositionRiskReport>[0],
  sessions: MarketSessionMap = SESSIONS,
) => buildPositionRiskReport(rows, { marketSessions: sessions })

describe('PositionRiskTable', () => {
  it('破位 / 逼近 / 未设防线 / 无现价 四种状态各自可见，缺现价不隐瞒', () => {
    mockCtx([])
    const report = reportOf([
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
    const report = reportOf([
      pos({ id: 1, symbol: '600111', defense_price: 33.96, current_price: 33.0 }),
      pos({ id: 2, symbol: '600519', defense_price: 1500, current_price: null, pnl_pct: null }),
    ])
    render(<PositionRiskTable report={report} />)

    expect(screen.getByTestId('position-risk-row-600111').textContent).toContain('-2.83%')
    expect(screen.getByTestId('position-risk-row-600519').textContent).toContain('--')
    expect(screen.getByTestId('position-risk-row-600519').textContent).toContain('无行情')
  })

  it('陈旧报价：显示真实行情日期与「时效未证实」，不写「已破位」也不写「刚刚」', () => {
    mockCtx([])
    const report = reportOf([
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
    expect(row.textContent).toContain('时效未证实')
    expect(row.textContent).toContain('2026-10-28')
    expect(row.textContent).not.toContain('已破位')
    expect(screen.getByTestId('position-risk-table').textContent).not.toContain('刚刚')
  })

  it('【三次评审】09:31 的报价在 14:03：不显示已破位，行情列写明滞后与报价时刻', () => {
    mockCtx([])
    const report = reportOf(
      [
        pos({
          id: 1,
          symbol: '600111',
          defense_price: 34,
          current_price: 33,
          quote_time: '2026-10-29 09:31:00',
        }),
      ],
      { CN: { localTime: '14:03', localDate: '2026-10-29', status: 'trading' } },
    )
    render(<PositionRiskTable report={report} />)
    const row = screen.getByTestId('position-risk-row-600111')
    expect(row.dataset.state).toBe('stale')
    expect(row.dataset.freshness).toBe('stale')
    expect(row.textContent).not.toContain('已破位')
    expect(row.textContent).toContain('时效未证实')
    const quoteCell = screen.getByTestId('position-risk-quote-600111').textContent ?? ''
    expect(quoteCell).toContain('滞后')
    expect(quoteCell).toContain('09:31')
  })

  it('【三次评审】没有报价时刻 / 没有服务器市场时钟：时点未知，不判破位', () => {
    mockCtx([])
    const noTime = reportOf([
      pos({ id: 1, symbol: '600111', defense_price: 34, current_price: 33, quote_time: null }),
    ])
    render(<PositionRiskTable report={noTime} />)
    const row = screen.getByTestId('position-risk-row-600111')
    expect(row.dataset.state).toBe('stale')
    expect(row.textContent).not.toContain('已破位')
    expect(screen.getByTestId('position-risk-quote-600111').textContent).toContain('时点未知')
    cleanup()

    const noClock = reportOf(
      [pos({ id: 1, symbol: '600111', defense_price: 34, current_price: 33 })],
      null,
    )
    render(<PositionRiskTable report={noClock} />)
    expect(screen.getByTestId('position-risk-row-600111').dataset.state).toBe('stale')
    expect(screen.getByTestId('position-risk-quote-600111').textContent).toContain('时点未证实')
  })

  it('同标的多账户：两行各自账户与防线，不合并成一个状态', () => {
    mockCtx([])
    const report = reportOf([
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
    const report = reportOf([
      pos({ id: 1, symbol: '00700', name: '腾讯控股', market: 'HK', current_price: 380, defense_price: 360 }),
      pos({ id: 2, symbol: 'AAPL', name: '苹果', market: 'US', current_price: 180, defense_price: 170 }),
    ])
    render(<PositionRiskTable report={report} />)
    expect(screen.getByTestId('position-risk-row-00700').textContent).toContain('HK$380')
    expect(screen.getByTestId('position-risk-row-AAPL').textContent).toContain('US$180')
  })

  it('不重复堆价格/成本/盈亏：表里不出现「成本」「浮亏」列（那两列在下方持仓表）', () => {
    mockCtx([])
    const report = reportOf([pos()])
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
    // 单位是「行」（账户内的持仓条目），不是「只」标的
    expect(text).toContain('已定价 0/1 行')
  })

  it('同标的多账户防线冲突：计数按账户算，并明说按账户分行、不做合并', () => {
    ctx.useStocks.mockReturnValue({
      portfolio: portfolioWith([], [
        account(1, '主账户', [pos({ id: 11, defense_price: 34, current_price: 33 })]),
        account(2, '备用账户', [pos({ id: 12, defense_price: 25, current_price: 33 })]),
      ]),
      viewTab: 'positions',
      marketStatus: MARKET_STATUS,
    })
    render(<PositionRiskSection />)
    const text = screen.getByTestId('position-risk-section').textContent ?? ''
    expect(text).toContain('已破位')
    // 2 个账户行、1 只标的：数字按行算，并注名标的数，不冒称「2 只」
    expect(text).toContain('已定价 2/2 行')
    expect(text).toContain('1 只标的')
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
    expect(text).toContain('时效未证实')
    expect(text).toContain('未计入破位')
  })

  it('【三次评审】指标数字与表格行数同源：时效未证实 = 表格里 stale 行数', () => {
    mockCtx([
      pos({ id: 1, symbol: '600111', defense_price: 34, current_price: 33, quote_time: '2026-10-29 09:31:00' }),
      pos({ id: 2, symbol: '600519', defense_price: 1500, current_price: 1490, quote_time: null }),
      pos({ id: 3, symbol: '000858', defense_price: 100, current_price: 101 }),
    ])
    render(<PositionRiskSection />)
    const section = screen.getByTestId('position-risk-section')
    const metric = section.querySelector('[data-testid="metric-stale"]')?.textContent ?? ''
    const staleRows = section.querySelectorAll('tbody tr[data-state="stale"]').length
    expect(metric).toContain(String(staleRows))
    expect(staleRows).toBe(2)
    // 陈旧的行不许进破位计数
    const breachedMetric = section.querySelector('[data-testid="metric-breached"]')?.textContent ?? ''
    expect(breachedMetric).toContain('0')
  })

  it('【三次评审】说明时效口径与「推断阈值」，并注明不是交易所口径', () => {
    mockCtx([pos()])
    render(<PositionRiskSection />)
    const rule = screen.getByTestId('position-risk-freshness-rule').textContent ?? ''
    expect(rule).toContain('15 分钟')
    expect(rule).toContain('推断值')
    expect(rule).toContain('市场时区')
  })

  it('【三次评审】JSX 里不许出现 markdown 星号（会原样渲染成 **xx**）', () => {
    mockCtx([pos({ current_price: null, defense_price: null, pnl_pct: null })])
    render(<PositionRiskSection />)
    const text = screen.getByTestId('position-risk-section').textContent ?? ''
    expect(text).not.toContain('**')
  })
})

/**
 * TQ「日线口径」（源只证到日，无成交时刻）的时间口径（2026-10-29 复审）：
 *  - 非交易时段（收盘后/午休/盘前）才允许拿这份**过去的成交价**核对防线，
 *    且必须写明「非实时」——它不是当下价，不许写成实时，也不许只写"最近报价"含糊过去；
 *  - 交易中仍然「时点未知」，不判破位。
 */
describe('日线口径（date_only）的措辞与红线', () => {
  const dateOnlyPos = (over: Record<string, unknown> = {}) =>
    pos({
      id: 1,
      defense_price: 34,
      current_price: 33,
      quote_date: '2026-10-29',
      quote_time: null,
      quote_time_kind: null,
      quote_date_basis: 'daily_bar_close',
      daily_pnl_period: 'today',
      ...over,
    })

  it('收盘后：说「最近成交价 + 日线口径 + 非实时」，不写实时', () => {
    mockCtx([], 'positions', [
      { code: 'CN', name: 'A股', status: 'after_hours', status_text: '已收盘', local_time: '19:40', local_date: '2026-10-29' },
    ])
    const report = reportOf([dateOnlyPos()], {
      CN: { localTime: '19:40', localDate: '2026-10-29', status: 'after_hours' },
    })
    render(<PositionRiskTable report={report} />)
    const row = screen.getByTestId('position-risk-row-600111')
    expect(row.textContent).toContain('最近成交价')
    expect(row.textContent).toContain('日线口径')
    expect(row.textContent).toContain('非实时')
    expect(row.textContent).toContain('无成交时刻')
  })

  it('交易中：同一份只有日期的证据仍是「时点未知」，不判破位', () => {
    mockCtx([])
    const report = reportOf([dateOnlyPos()])
    render(<PositionRiskTable report={report} />)
    const row = screen.getByTestId('position-risk-row-600111')
    expect(row.dataset.state).not.toBe('breached')
    expect(row.textContent).toContain('时点未知')
    expect(row.textContent).not.toContain('最近成交价')
  })
})
