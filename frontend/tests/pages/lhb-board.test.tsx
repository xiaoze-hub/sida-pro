// @vitest-environment jsdom
/**
 * 龙虎榜独立工作区(先红后绿)。
 *
 * 这组用例对应四个真实会出错的场景, 不是为了覆盖率:
 *  ① **请求乱序**: 连点两个日期/两只股票时, 先发的慢响应不能覆盖后发的快响应
 *     (旧代码最容易犯: 直接 setState(await ...) ⇒ 用户看到的日期与数据对不上);
 *  ② **多上榜原因不可盲加**: 同股同日两条原因, 页面必须分条展示、明确不做加总;
 *  ③ **空/失败**: 空榜给"无覆盖"原因, 失败给可重试错误态, 绝不伪造数据;
 *  ④ **营业部 ≠ 已验证游资**: 席位面板必须有这句身份声明, 且金额标注元/万/亿口径。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

const archiveApi = vi.hoisted(() => ({
  overview: vi.fn(),
  dragonTiger: vi.fn(),
  lhbSeats: vi.fn(),
}))
vi.mock('@sida/api', () => ({ archiveApi }))

import LhbBoardPage from '@/pages/LhbBoard'

/** 参谋落点探针: 断言页面真的跳到了 /ai-copilot, 且查询串是页面自己拼的。 */
function CopilotProbe() {
  const loc = useLocation()
  return <div data-testid="copilot-probe" data-path={loc.pathname} data-search={loc.search} />
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/lhb']}>
      <Routes>
        <Route path="/lhb" element={<LhbBoardPage />} />
        <Route path="/ai-copilot" element={<CopilotProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

interface Deferred<T> {
  promise: Promise<T>
  resolve: (v: T) => void
  reject: (e: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const overview = (latest: string | null) => ({
  tables: [
    { table: 'quote_snapshots', rows: 1, rows_estimated: false, earliest_date: latest, latest_date: latest },
    { table: 'dragon_tiger_events', rows: 2, rows_estimated: false, earliest_date: latest, latest_date: latest },
  ],
  redis_cache: { keyspace_hits: null, keyspace_misses: null, hit_rate: null },
  note: '',
})

const dt = (over: Record<string, unknown> = {}) => ({
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
  free_market_cap: 300_000_000_000,
  source: 'eastmoney',
  created_at: '2026-11-06T18:00:00',
  ...over,
})

const seat = (over: Record<string, unknown> = {}) => ({
  row_uid: 'r1',
  trade_date: '20261106',
  side: 'buy',
  symbol: '600000',
  name: '浦发银行',
  reason: '日涨幅偏离值达7%',
  operate_dept_code: '1001',
  operate_dept_name: '某某证券股份有限公司上海某营业部',
  close: 10.5,
  change_pct: 8.1,
  accum_amount: 900_000_000,
  accum_volume: 0,
  buy_amt: 80_000_000,
  sell_amt: 0,
  net_amt: 80_000_000,
  rise_probability_3d: 55.5,
  change_type: '1',
  trade_id: 't1',
  total_buy_ratio: 8.8,
  total_sell_ratio: 0,
  source: 'eastmoney',
  created_at: '2026-11-06T18:00:00',
  ...over,
})

const dayOk = (items: unknown[]) => ({ count: items.length, note: items.length ? '' : '空=无覆盖', items })
const seatsOk = (items: unknown[]) => ({ count: items.length, note: items.length ? '' : '空=无覆盖', items })

beforeEach(() => {
  archiveApi.overview.mockReset()
  archiveApi.dragonTiger.mockReset()
  archiveApi.lhbSeats.mockReset()
  archiveApi.overview.mockResolvedValue(overview('20261106'))
  archiveApi.lhbSeats.mockResolvedValue(seatsOk([]))
})
afterEach(() => cleanup())

describe('龙虎榜工作区 · 日榜', () => {
  it('默认落在数据最新日期, 同股多条上榜原因分条展示且不做金额加总', async () => {
    archiveApi.dragonTiger.mockResolvedValue(
      dayOk([
        dt({ reason: '日涨幅偏离值达7%', net_buy: 120_000_000 }),
        dt({ reason: '连续三个交易日涨幅偏离值达20%', net_buy: 120_000_000 }),
      ]),
    )
    renderPage()

    const dateInput = (await screen.findByTestId('lhb-date')) as HTMLInputElement
    await waitFor(() => expect(dateInput.value).toBe('2026-11-06'))
    expect(archiveApi.dragonTiger).toHaveBeenCalledWith(expect.objectContaining({ date: '20261106' }))

    const stock = await screen.findByTestId('lhb-stock-600000')
    expect(within(stock).getByText('日涨幅偏离值达7%')).toBeTruthy()
    expect(within(stock).getByText('连续三个交易日涨幅偏离值达20%')).toBeTruthy()
    // 两条原因各 1.20亿, 加总会变 2.40亿 —— 必须不出现(正则: 带不带符号都算命中)
    expect(within(stock).getAllByText(/1\.20亿/).length).toBeGreaterThan(0)
    expect(within(stock).queryByText(/2\.40亿/)).toBeNull()
    expect(screen.getByTestId('lhb-multi-reason-note').textContent).toContain('不做加总')
    // 金额单位口径必须写清(后端是元)
    expect(screen.getByTestId('lhb-unit-note').textContent).toContain('元')
  })

  it('切日期请求乱序: 先发的慢响应回来时不能覆盖当前选中日期的数据', async () => {
    const slow = deferred<ReturnType<typeof dayOk>>()
    const fast = deferred<ReturnType<typeof dayOk>>()
    archiveApi.dragonTiger.mockImplementation(({ date }: { date: string }) =>
      date === '20261106' ? slow.promise : fast.promise,
    )
    renderPage()

    const dateInput = (await screen.findByTestId('lhb-date')) as HTMLInputElement
    await waitFor(() => expect(dateInput.value).toBe('2026-11-06'))

    fireEvent.change(dateInput, { target: { value: '2026-11-05' } })
    await waitFor(() => expect(archiveApi.dragonTiger).toHaveBeenLastCalledWith(expect.objectContaining({ date: '20261105' })))
    fast.resolve(dayOk([dt({ trade_date: '20261105', symbol: '000001', name: '平安银行' })]))
    expect(await screen.findByTestId('lhb-stock-000001')).toBeTruthy()

    // 旧请求(20261106)现在才回来 —— 必须被丢弃
    slow.resolve(dayOk([dt({ symbol: '600000', name: '浦发银行' })]))
    await waitFor(() => expect(screen.getByTestId('lhb-source-date').textContent).toContain('2026-11-05'))
    expect(screen.queryByTestId('lhb-stock-600000')).toBeNull()
    expect(screen.getByTestId('lhb-stock-000001')).toBeTruthy()
  })

  it('空榜 → 明确空态 + 透出后端原因, 不渲染假行', async () => {
    archiveApi.dragonTiger.mockResolvedValue({ count: 0, note: '空=无覆盖(非交易日)', items: [] })
    renderPage()
    const empty = await screen.findByTestId('lhb-empty')
    expect(empty.textContent).toContain('无覆盖')
    expect(screen.queryByTestId('lhb-stock-list')).toBeNull()
  })

  it('失败 → 可重试错误态; 重试成功后恢复数据', async () => {
    archiveApi.dragonTiger
      .mockRejectedValueOnce(new Error('archive 502'))
      .mockResolvedValueOnce(dayOk([dt()]))
    renderPage()
    const err = await screen.findByTestId('lhb-error')
    expect(err.textContent).toContain('502')

    fireEvent.click(screen.getByTestId('lhb-retry'))
    expect(await screen.findByTestId('lhb-stock-600000')).toBeTruthy()
    expect(screen.queryByTestId('lhb-error')).toBeNull()
  })
})

describe('龙虎榜工作区 · 席位', () => {
  it('选股后分买方/卖方展示营业部, 并把来源日期写清', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    archiveApi.lhbSeats.mockResolvedValue(
      seatsOk([
        seat({ row_uid: 'b1', side: 'buy', net_amt: 80_000_000 }),
        seat({ row_uid: 's1', side: 'sell', operate_dept_name: '另一证券股份有限公司深圳某营业部', net_amt: 30_000_000 }),
      ]),
    )
    renderPage()
    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))

    const buy = await screen.findByTestId('lhb-seats-buy')
    expect(buy.textContent).toContain('某某证券股份有限公司上海某营业部')
    const sell = screen.getByTestId('lhb-seats-sell')
    expect(sell.textContent).toContain('另一证券股份有限公司深圳某营业部')
    expect(screen.getByTestId('lhb-seats-panel').textContent).toContain('2026-11-06')
    expect(archiveApi.lhbSeats).toHaveBeenCalledWith({ date: '20261106', symbol: '600000' })
  })

  it('营业部 ≠ 已验证游资: 席位面板常驻身份声明', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    archiveApi.lhbSeats.mockResolvedValue(seatsOk([seat()]))
    renderPage()
    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))
    const note = await screen.findByTestId('lhb-seat-disclaimer')
    expect(note.textContent).toContain('营业部席位')
    expect(note.textContent).toContain('不等于已验证游资')
  })

  it('切股票席位乱序: 慢的旧股票席位不能盖住当前选中股票', async () => {
    archiveApi.dragonTiger.mockResolvedValue(
      dayOk([dt({ symbol: '600000' }), dt({ symbol: '000001', name: '平安银行' })]),
    )
    const slow = deferred<ReturnType<typeof seatsOk>>()
    const fast = deferred<ReturnType<typeof seatsOk>>()
    archiveApi.lhbSeats.mockImplementation(({ symbol }: { symbol: string }) =>
      symbol === '600000' ? slow.promise : fast.promise,
    )
    renderPage()

    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))
    fireEvent.click(await screen.findByTestId('lhb-stock-000001'))
    fast.resolve(seatsOk([seat({ symbol: '000001', operate_dept_name: '平安证券某营业部' })]))
    expect(await screen.findByText('平安证券某营业部')).toBeTruthy()

    slow.resolve(seatsOk([seat({ symbol: '600000', operate_dept_name: '过期营业部' })]))
    await waitFor(() => expect(screen.queryByText('过期营业部')).toBeNull())
    expect(screen.getByText('平安证券某营业部')).toBeTruthy()
  })

  it('席位为空 → 明确"未披露/无明细", 不编造营业部', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    archiveApi.lhbSeats.mockResolvedValue({ count: 0, note: '空=无覆盖(榜单无营业部明细)', items: [] })
    renderPage()
    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))
    const empty = await screen.findByTestId('lhb-seats-empty')
    expect(empty.textContent).toContain('无')
    expect(screen.queryByTestId('lhb-seats-buy')).toBeNull()
  })

  it('席位请求失败 → 席位面板单独报错并可重试, 日榜仍可见', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    archiveApi.lhbSeats
      .mockRejectedValueOnce(new Error('seats 500'))
      .mockResolvedValueOnce(seatsOk([seat()]))
    renderPage()
    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))
    const err = await screen.findByTestId('lhb-seats-error')
    expect(err.textContent).toContain('500')
    expect(screen.getByTestId('lhb-stock-600000')).toBeTruthy()

    fireEvent.click(screen.getByTestId('lhb-seats-retry'))
    expect(await screen.findByText('某某证券股份有限公司上海某营业部')).toBeTruthy()
  })
})

describe('龙虎榜工作区 · 覆盖口径(无完整性凭据)', () => {
  it('有数据时仍常驻「覆盖未核实」, 且数据日期单独标注, 不承诺完整', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    renderPage()
    const chip = await screen.findByTestId('lhb-quality-note')
    expect(chip.textContent).toContain('覆盖未核实')
    // 不得把入库记录说成"完整榜单/今日已发布"
    expect(chip.textContent).not.toContain('完整榜单')
    expect(screen.getByTestId('lhb-source-date').textContent).toContain('2026-11-06')
    expect(screen.getByTestId('lhb-page').textContent).not.toContain('完整榜单')
  })

  it('空榜同样标「覆盖未核实」: 归档无记录不等于当日没有龙虎榜', async () => {
    archiveApi.dragonTiger.mockResolvedValue({ count: 0, note: '空=无覆盖(非交易日)', items: [] })
    renderPage()
    const chip = await screen.findByTestId('lhb-quality-note')
    expect(chip.textContent).toContain('覆盖未核实')
    expect(chip.textContent).toContain('不代表')
  })
})

describe('龙虎榜工作区 · 多原因行金额不串行', () => {
  it('两条原因各自带自己的买卖净额, 不互相串值', async () => {
    archiveApi.dragonTiger.mockResolvedValue(
      dayOk([
        dt({ reason: '日涨幅偏离值达7%', net_buy: 120_000_000, buy_amt: 500_000_000, sell_amt: 380_000_000 }),
        dt({ reason: '日换手率达20%', net_buy: 30_000_000, buy_amt: 90_000_000, sell_amt: 60_000_000 }),
      ]),
    )
    renderPage()
    const first = await screen.findByTestId('lhb-reason-600000-0')
    const second = screen.getByTestId('lhb-reason-600000-1')

    expect(first.textContent).toContain('日涨幅偏离值达7%')
    expect(first.textContent).toContain('1.20亿')
    expect(first.textContent).toContain('5.00亿')
    expect(first.textContent).toContain('3.80亿')

    expect(second.textContent).toContain('日换手率达20%')
    expect(second.textContent).toContain('3000.00万')
    expect(second.textContent).toContain('9000.00万')
    expect(second.textContent).toContain('6000.00万')
    // 第二行不得出现第一行的读数
    expect(second.textContent).not.toContain('1.20亿')
    expect(second.textContent).not.toContain('5.00亿')
    expect(second.textContent).not.toContain('3.80亿')
  })
})

describe('龙虎榜工作区 · 分析此股龙虎榜(参谋真实入口)', () => {
  const probeParams = () => {
    const el = screen.getByTestId('copilot-probe')
    return new URLSearchParams(el.getAttribute('data-search') || '')
  }

  it('未选股时不渲染分析入口: 没有股票/日期就不造上下文', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    renderPage()
    await screen.findByTestId('lhb-stock-600000')
    expect(screen.queryByTestId('lhb-analyze-copilot')).toBeNull()
  })

  it('选股后点「分析此股龙虎榜」→ /ai-copilot, 只带 symbol 与含明确日期的 q', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    archiveApi.lhbSeats.mockResolvedValue(seatsOk([seat()]))
    renderPage()

    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))
    fireEvent.click(await screen.findByTestId('lhb-analyze-copilot'))

    const probe = await screen.findByTestId('copilot-probe')
    expect(probe.getAttribute('data-path')).toBe('/ai-copilot')
    const sp = probeParams()
    expect(sp.get('symbol')).toBe('600000')
    expect(sp.get('q')).toContain('2026-11-06')
    expect(sp.get('q')).toContain('600000')
    // 入口没有龙虎榜配方时不许硬塞 agent, 也不许塞被入口忽略的 date
    expect(sp.has('agent')).toBe(false)
    expect(sp.has('date')).toBe(false)
  })

  it('切股票后分析入口跟随当前股票, 不沿用上一只', async () => {
    archiveApi.dragonTiger.mockResolvedValue(
      dayOk([dt({ symbol: '600000' }), dt({ symbol: '000001', name: '平安银行' })]),
    )
    renderPage()

    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))
    fireEvent.click(await screen.findByTestId('lhb-stock-000001'))
    fireEvent.click(await screen.findByTestId('lhb-analyze-copilot'))

    const sp = probeParams()
    expect(sp.get('symbol')).toBe('000001')
    expect(sp.get('q')).toContain('平安银行')
    expect(sp.get('q')).not.toContain('600000')
  })

  it('切日期后分析入口用当前榜单日期, 旧日期的选中股票不会带过去', async () => {
    archiveApi.dragonTiger.mockImplementation(({ date }: { date: string }) =>
      Promise.resolve(
        date === '20261106'
          ? dayOk([dt({ symbol: '600000' })])
          : dayOk([dt({ trade_date: '20261105', symbol: '000001', name: '平安银行' })]),
      ),
    )
    renderPage()

    const dateInput = (await screen.findByTestId('lhb-date')) as HTMLInputElement
    await waitFor(() => expect(dateInput.value).toBe('2026-11-06'))
    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))
    expect(screen.getByTestId('lhb-analyze-copilot')).toBeTruthy()

    fireEvent.change(dateInput, { target: { value: '2026-11-05' } })
    expect(await screen.findByTestId('lhb-stock-000001')).toBeTruthy()
    // 换日后旧选中股票被清掉 → 分析入口消失, 必须重新选股
    expect(screen.queryByTestId('lhb-analyze-copilot')).toBeNull()

    fireEvent.click(screen.getByTestId('lhb-stock-000001'))
    fireEvent.click(await screen.findByTestId('lhb-analyze-copilot'))
    const sp = probeParams()
    expect(sp.get('symbol')).toBe('000001')
    expect(sp.get('q')).toContain('2026-11-05')
    expect(sp.get('q')).not.toContain('2026-11-06')
  })
})
