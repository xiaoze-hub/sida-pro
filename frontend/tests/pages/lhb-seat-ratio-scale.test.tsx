// @vitest-environment jsdom
/**
 * 龙虎榜 · 席位占比量纲验收(先红后绿, 2026-11-09)。
 *
 * 生产只读对拍结论(不是猜): `lhb_seat_details.total_buy_ratio` / `total_sell_ratio`
 * 落库的是 **0~1 小数**(如 0.1981 = 19.81%), 而**不是**百分点; 同表
 * `rise_probability_3d` 落库的是**百分点**(如 55.5 = 55.5%), 不能再乘 100。
 *
 * 旧代码把三个字段一律当百分点交给 `safePercent` ⇒ 占比显示被缩小 100 倍
 * (0.1981 → "0.20%"), 本组用例锁死这个口径, 并覆盖:
 *  ① 买侧占比: 0.1981 → 19.81%;
 *  ② 卖侧占比: 0.0345 → 3.45%(卖侧读 total_sell_ratio, 不能串到买侧);
 *  ③ 0 → "0.00%"(不是 "--"; 0 是真实数据, 缺失才是未知);
 *  ④ null → "--"(缺失不冒充 0);
 *  ⑤ 后端 DECIMAL 经 JSON 变字符串: "0.1981" 同样 19.81%;
 *  ⑥ `rise_probability_3d` 已是百分点: 55.5 → 55.50%, 绝不出 "5550.00%"。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const archiveApi = vi.hoisted(() => ({
  overview: vi.fn(),
  dragonTiger: vi.fn(),
  lhbSeats: vi.fn(),
}))
vi.mock('@sida/api', () => ({ archiveApi }))

import LhbBoardPage from '@/pages/LhbBoard'

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/lhb']}>
      <Routes>
        <Route path="/lhb" element={<LhbBoardPage />} />
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

const dt = {
  trade_date: '20261106',
  symbol: '600000',
  market: 'CN',
  name: '浦发银行',
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
}

const seat = (over: Record<string, unknown> = {}) => ({
  row_uid: 'r1',
  trade_date: '20261106',
  side: 'buy',
  symbol: '600000',
  name: '浦发银行',
  reason: '日涨幅偏离值达7%',
  operate_dept_code: '1001',
  operate_dept_name: '某某证券股份有限公司上海某营业部',
  buy_amt: 80_000_000,
  sell_amt: 0,
  net_amt: 80_000_000,
  rise_probability_3d: 55.5,
  total_buy_ratio: 0.1981,
  total_sell_ratio: 0.0345,
  source: 'eastmoney',
  created_at: '2026-11-06T18:00:00',
  ...over,
})

/** 选中个股并等席位列表渲染出来, 返回该席位行的文本。 */
async function seatText(side: 'buy' | 'sell', rowUid = 'r1'): Promise<string> {
  fireEvent.click(await screen.findByTestId('lhb-stock-600000'))
  const row = await screen.findByTestId(`lhb-seat-${side}-${rowUid}`)
  return row.textContent ?? ''
}

beforeEach(() => {
  archiveApi.overview.mockReset()
  archiveApi.dragonTiger.mockReset()
  archiveApi.lhbSeats.mockReset()
  archiveApi.overview.mockResolvedValue(overview())
  archiveApi.dragonTiger.mockResolvedValue({
    count: 1,
    note: '',
    items: [dt],
    quality: { status: 'unverified', severity: 'caution', label: '未经核对', note: '不声明完整' },
  })
  archiveApi.lhbSeats.mockResolvedValue({ count: 0, note: '', items: [] })
})
afterEach(() => cleanup())

describe('席位占比量纲: 库内 0~1 小数 → 页面百分点', () => {
  it('买侧 0.1981 → 19.81%(不是 0.20%)', async () => {
    archiveApi.lhbSeats.mockResolvedValue({ count: 1, note: '', items: [seat()] })
    renderPage()
    const text = await seatText('buy')
    expect(text).toContain('占买入总额 19.81%')
    expect(text).not.toContain('0.20%')
  })

  it('卖侧读 total_sell_ratio: 0.0345 → 3.45%, 不串到买侧占比', async () => {
    archiveApi.lhbSeats.mockResolvedValue({
      count: 1,
      note: '',
      items: [seat({ side: 'sell', total_buy_ratio: 0.9, total_sell_ratio: 0.0345 })],
    })
    renderPage()
    const text = await seatText('sell')
    expect(text).toContain('占卖出总额 3.45%')
    expect(text).not.toContain('90.00%')
  })

  it('0 → 0.00%(真实零值, 不是缺失的 --)', async () => {
    archiveApi.lhbSeats.mockResolvedValue({
      count: 1,
      note: '',
      items: [seat({ total_buy_ratio: 0, total_sell_ratio: 0 })],
    })
    renderPage()
    const text = await seatText('buy')
    expect(text).toContain('占买入总额 0.00%')
    expect(text).not.toContain('占买入总额 --')
  })

  it('null → --(缺失不冒充 0%)', async () => {
    archiveApi.lhbSeats.mockResolvedValue({
      count: 1,
      note: '',
      items: [seat({ total_buy_ratio: null })],
    })
    renderPage()
    const text = await seatText('buy')
    expect(text).toContain('占买入总额 --')
    expect(text).not.toContain('占买入总额 0.00%')
  })

  it('DECIMAL 字符串 "0.1981" 同样 19.81%', async () => {
    archiveApi.lhbSeats.mockResolvedValue({ count: 1, note: '', items: [seat({ total_buy_ratio: '0.1981' })] })
    renderPage()
    const text = await seatText('buy')
    expect(text).toContain('占买入总额 19.81%')
  })

  it('3日上涨概率已是百分点: 55.5 → 55.50%, 不乘 100', async () => {
    archiveApi.lhbSeats.mockResolvedValue({ count: 1, note: '', items: [seat({ rise_probability_3d: 55.5 })] })
    renderPage()
    const text = await seatText('buy')
    expect(text).toContain('3日上涨概率 55.50%')
    expect(text).not.toContain('5550')
  })

  it('3日上涨概率缺失: 不渲染该字段(不冒充 0%)', async () => {
    archiveApi.lhbSeats.mockResolvedValue({ count: 1, note: '', items: [seat({ rise_probability_3d: null })] })
    renderPage()
    const text = await seatText('buy')
    expect(text).not.toContain('3日上涨概率')
  })
})
