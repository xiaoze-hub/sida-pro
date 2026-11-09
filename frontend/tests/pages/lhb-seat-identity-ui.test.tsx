// @vitest-environment jsdom
/**
 * 龙虎榜 · 席位身份与北交所文案的组件级验收(先红后绿, 2026-11-09 复审第二轮)。
 *
 * 渲染**真组件** `LhbBoardPage`, 只 mock `archiveApi`; 断言用户真正看到的字:
 *  ① 三条「机构专用」: 页面只写「3 条记录 · 身份未知」, 不得写「1 家营业部」;
 *  ② 北交所 920002 点「分析此股龙虎榜」: 文案不得出现 "非股票"/"ETF"/"可转债",
 *     只说入口没把它识别为股票锚点, 代码只写在问题正文里;
 *  ③ 真营业部仍按代码去重报家数(修复不能把正常路径一起打瘸)。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

const archiveApi = vi.hoisted(() => ({
  overview: vi.fn(),
  dragonTiger: vi.fn(),
  lhbSeats: vi.fn(),
}))
vi.mock('@sida/api', () => ({ archiveApi }))

import LhbBoardPage from '@/pages/LhbBoard'

function CopilotProbe() {
  const loc = useLocation()
  return <div data-testid="copilot-probe" data-path={loc.pathname} data-search={loc.search} />
}

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/lhb']}>
      <Routes>
        <Route path="/lhb" element={<LhbBoardPage />} />
        <Route path="/ai-copilot" element={<CopilotProbe />} />
      </Routes>
    </MemoryRouter>,
  )

const overview = () => ({
  tables: [
    { table: 'dragon_tiger_events', rows: 1, rows_estimated: false, earliest_date: '20261106', latest_date: '20261106' },
  ],
  redis_cache: { keyspace_hits: null, keyspace_misses: null, hit_rate: null },
  note: '',
})

const dt = (over: Record<string, unknown> = {}) => ({
  trade_date: '20261106',
  symbol: '920002',
  market: 'CN',
  name: '万达轴承',
  reason: '日涨幅偏离值达7%',
  close: 10.5,
  change_pct: 8.1,
  net_buy: 120_000_000,
  buy_amt: 500_000_000,
  sell_amt: 380_000_000,
  deal_amt: 900_000_000,
  turnover_pct: 3.2,
  source: 'eastmoney',
  created_at: '2026-11-06T18:00:00',
  ...over,
})

const seat = (over: Record<string, unknown> = {}) => ({
  row_uid: 'r1',
  trade_date: '20261106',
  side: 'buy',
  symbol: '920002',
  name: '万达轴承',
  reason: '日涨幅偏离值达7%',
  operate_dept_code: '1001',
  operate_dept_name: '某某证券股份有限公司上海某营业部',
  buy_amt: 80_000_000,
  sell_amt: 0,
  net_amt: 80_000_000,
  total_buy_ratio: 8.8,
  total_sell_ratio: 0,
  source: 'eastmoney',
  created_at: '2026-11-06T18:00:00',
  ...over,
})

beforeEach(() => {
  archiveApi.overview.mockReset()
  archiveApi.dragonTiger.mockReset()
  archiveApi.lhbSeats.mockReset()
  archiveApi.overview.mockResolvedValue(overview())
  archiveApi.dragonTiger.mockResolvedValue({
    count: 1,
    note: '',
    items: [dt()],
    quality: { status: 'unverified', severity: 'caution', label: '未经核对', note: '不声明完整' },
  })
  archiveApi.lhbSeats.mockResolvedValue({ count: 0, note: '', items: [] })
})
afterEach(() => cleanup())

describe('席位家数: 机构专用不能算成一家', () => {
  it('3 条「机构专用」→ 「3 条记录 · 身份未知」, 不出现「1 家营业部」', async () => {
    archiveApi.lhbSeats.mockResolvedValue({
      count: 3,
      note: '',
      items: [
        seat({ row_uid: 'a', operate_dept_name: '机构专用' }),
        seat({ row_uid: 'b', operate_dept_name: '机构专用' }),
        seat({ row_uid: 'c', operate_dept_name: '机构专用' }),
      ],
    })
    renderPage()
    fireEvent.click(await screen.findByTestId('lhb-stock-920002'))

    const count = await screen.findByTestId('lhb-seat-count-buy')
    expect(count.textContent).toContain('3 条记录')
    expect(count.textContent).toContain('身份未知')
    expect(count.textContent).not.toContain('家营业部')

    const note = screen.getByTestId('lhb-seat-identity-note')
    expect(note.textContent).toContain('机构专用')
    expect(note.textContent).not.toMatch(/1 家营业部/)

    // 三条记录仍逐条列出, 不因计数口径吞行
    const buy = screen.getByTestId('lhb-seats-buy')
    expect(buy.textContent).toContain('机构专用')
    expect(buy.querySelectorAll('[data-testid^="lhb-seat-buy-"]').length).toBe(3)
  })

  it('真营业部(同代码两原因行)仍报「2 条记录 · 1 家营业部」, 且不显示身份未知说明', async () => {
    archiveApi.lhbSeats.mockResolvedValue({
      count: 2,
      note: '',
      items: [
        seat({ row_uid: 'a', reason: '日涨幅偏离值达7%' }),
        seat({ row_uid: 'b', reason: '连续三个交易日涨幅偏离值达20%' }),
      ],
    })
    renderPage()
    fireEvent.click(await screen.findByTestId('lhb-stock-920002'))

    const count = await screen.findByTestId('lhb-seat-count-buy')
    expect(count.textContent).toContain('2 条记录')
    expect(count.textContent).toContain('1 家营业部')
    expect(screen.queryByTestId('lhb-seat-identity-note')).toBeNull()
  })
})

describe('北交所 920002 的参谋交接文案: 不猜证券类型', () => {
  it('提示里不说非股票/ETF/可转债, 只说没被识别为股票锚点', async () => {
    renderPage()
    fireEvent.click(await screen.findByTestId('lhb-stock-920002'))

    const hint = await screen.findByTestId('lhb-analyze-hint')
    expect(hint.textContent).toContain('920002')
    expect(hint.textContent).toContain('锚点')
    expect(hint.textContent).toContain('预填')
    expect(hint.textContent).not.toContain('非股票')
    expect(hint.textContent).not.toContain('ETF')
    expect(hint.textContent).not.toContain('可转债')

    fireEvent.click(screen.getByTestId('lhb-analyze-copilot'))
    const probe = await screen.findByTestId('copilot-probe')
    expect(probe.getAttribute('data-path')).toBe('/ai-copilot')
    const sp = new URLSearchParams(probe.getAttribute('data-search') || '')
    expect(sp.has('symbol')).toBe(false)
    expect(sp.get('q')).toContain('920002')
    expect(sp.get('q')).toContain('2026-11-06')
  })
})
