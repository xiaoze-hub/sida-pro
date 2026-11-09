// @vitest-environment jsdom
/**
 * 龙虎榜工作区 · 复审修复的组件级验收(先红后绿, 2026-11-09)。
 *
 * 这里渲染**真组件**, 只把 `archiveApi` 换成可控返回值。断言的是用户实际看到的字:
 *  ① 后端 quality 的定性/风险/交叉提示真的出现在页面上; 没给时是"未知", 不是"完整";
 *  ② 入口不认的代码点「分析此股龙虎榜」: 不带 symbol 锚点, 页面明说没被识别为股票锚点
 *     (第二轮起改为不猜证券类型的中性文案, 见 lhb-seat-identity-ui.test.tsx);
 *  ③ 文案明确是"预填、需手动发送", 不是自动发送;
 *  ④ 同营业部多原因多行时, 显示"条记录/家营业部", 不写"N 席"。
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

const overview = (latest: string | null) => ({
  tables: [
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
  buy_amt: 80_000_000,
  sell_amt: 0,
  net_amt: 80_000_000,
  total_buy_ratio: 8.8,
  total_sell_ratio: 0,
  source: 'eastmoney',
  created_at: '2026-11-06T18:00:00',
  ...over,
})

const quality = (over: Record<string, unknown> = {}) => ({
  table: 'dragon_tiger_events',
  trade_date: '20261106',
  market: 'CN',
  status: 'unverified',
  complete: false,
  degraded: false,
  verification: 'unverified',
  severity: 'caution',
  label: '未经核对',
  completeness_claimed: false,
  verified_against_official: false,
  actual_symbols: 2,
  first_write_at: '2026-11-06 15:02:11',
  last_write_at: '2026-11-06 15:04:03',
  publish_ready_after: '16:00',
  publish_ready_basis: '经验下界',
  early_freeze: true,
  early_freeze_risk: true,
  early_freeze_note: '早写风险: created_at 最早 2026-11-06 15:02 早于经验下界 16:00(北京)。',
  cross_check: {
    daily_symbols: 3,
    only_in_events: 1,
    only_in_daily: 2,
    note: '双向差异仅作交叉提示: 两表均非官方完整清单, 不能据此判定任何一方完整',
  },
  warnings: ['early_freeze_risk: 最早写入 2026-11-06 15:02 早于发布就绪经验下界 16:00, 风险提示。'],
  note: 'dragon_tiger_events 为定时回源落库的独立来源, 无官方完整清单可核对: 不声明完整, 也无证据表明残缺。',
  ...over,
})

const dayOk = (items: unknown[], q?: Record<string, unknown> | null) => ({
  count: items.length,
  note: items.length ? '' : '空=无覆盖',
  items,
  ...(q === null ? {} : { quality: q ?? quality() }),
})

beforeEach(() => {
  archiveApi.overview.mockReset()
  archiveApi.dragonTiger.mockReset()
  archiveApi.lhbSeats.mockReset()
  archiveApi.overview.mockResolvedValue(overview('20261106'))
  archiveApi.lhbSeats.mockResolvedValue({ count: 0, note: '空=无覆盖', items: [] })
})
afterEach(() => cleanup())

const probeParams = () => {
  const el = screen.getByTestId('copilot-probe')
  return new URLSearchParams(el.getAttribute('data-search') || '')
}

describe('后端 quality: 真实缺口与风险必须被接收并显示', () => {
  it('定性标签/不声明完整/早写风险/交叉提示/写入窗口都出现在页面上', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    renderPage()

    // 数据未就绪前 chip 是"未知"; 等质量说明出现(ready)再断言
    const note = await screen.findByTestId('lhb-quality-note')
    const chip = screen.getByTestId('lhb-quality-chip')
    expect(chip.textContent).toContain('未经核对')

    expect(note.textContent).toContain('不声明完整')

    const risks = screen.getByTestId('lhb-quality-risks')
    expect(risks.textContent).toContain('最早写入')
    expect(risks.textContent).toContain('风险提示')

    const cross = screen.getByTestId('lhb-quality-crosscheck')
    expect(cross.textContent).toContain('不能据此判定任何一方完整')
    expect(cross.textContent).toContain('多 1 只')
    expect(cross.textContent).toContain('少 2 只')

    const win = screen.getByTestId('lhb-quality-write-window')
    expect(win.textContent).toContain('15:02')
    expect(win.textContent).toContain('16:00')

    // 有数据也不许承诺完整
    expect(screen.getByTestId('lhb-page').textContent).not.toContain('完整榜单')
    expect(screen.getByTestId('lhb-page').textContent).not.toContain('已确认完整')
  })

  it('后端没给 quality → 保持未知, 不显示风险/交叉提示, 也不暗示完整', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()], null))
    renderPage()

    const note = await screen.findByTestId('lhb-quality-note')
    const chip = screen.getByTestId('lhb-quality-chip')
    expect(chip.textContent).toContain('覆盖未核实')
    expect(note.textContent).toContain('未知')
    expect(screen.queryByTestId('lhb-quality-risks')).toBeNull()
    expect(screen.queryByTestId('lhb-quality-crosscheck')).toBeNull()
    expect(screen.queryByTestId('lhb-quality-write-window')).toBeNull()
    expect(screen.getByTestId('lhb-page').textContent).not.toContain('完整榜单')
  })

  it('后端声称 complete 但未经官方核对: 页面明说不采信, 不写「已核对完整」', async () => {
    archiveApi.dragonTiger.mockResolvedValue(
      dayOk([dt()], quality({ status: 'complete', complete: true, completeness_claimed: true, label: '完整' })),
    )
    renderPage()

    const note = await screen.findByTestId('lhb-quality-note')
    const chip = screen.getByTestId('lhb-quality-chip')
    expect(chip.textContent).not.toContain('已核对完整')
    expect(note.textContent).toContain('不采信')
    expect(screen.getByTestId('lhb-page').textContent).not.toContain('完整榜单')
  })

  it('absent 定性: 空态写「无快照」并说明无覆盖 ≠ 当日没有龙虎榜', async () => {
    archiveApi.dragonTiger.mockResolvedValue(
      dayOk([], quality({ status: 'absent', severity: 'missing', degraded: true, label: '无快照', actual_symbols: 0 })),
    )
    renderPage()

    const empty = await screen.findByTestId('lhb-empty')
    expect(empty.textContent).toContain('无龙虎榜数据')
    const note = screen.getByTestId('lhb-quality-note')
    expect(note.textContent).toContain('无快照')
    expect(note.textContent).toContain('不等于')
  })

  it('切日期加载中不沿用上一日的风险定性(不串日期)', async () => {
    let resolveNext: (v: unknown) => void = () => {}
    archiveApi.dragonTiger.mockImplementation(({ date }: { date: string }) =>
      date === '20261106'
        ? Promise.resolve(dayOk([dt()]))
        : new Promise((resolve) => {
            resolveNext = resolve
          }),
    )
    renderPage()

    // 第一日: 有早写风险
    expect(await screen.findByTestId('lhb-quality-risks')).toBeTruthy()

    const dateInput = (await screen.findByTestId('lhb-date')) as HTMLInputElement
    fireEvent.change(dateInput, { target: { value: '2026-11-05' } })
    await waitFor(() => expect(screen.queryByTestId('lhb-quality-risks')).toBeNull())
    expect(screen.getByTestId('lhb-quality-chip').textContent).toContain('覆盖未核实')
    expect(screen.queryByTestId('lhb-quality-write-window')).toBeNull()

    resolveNext({ count: 0, note: '', items: [], quality: null })
  })
})

describe('席位条数: 同营业部多原因多行不得说成独立席位', () => {
  it('2 行同营业部 → 「2 条记录 / 1 家营业部」并说明不等于独立席位', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    archiveApi.lhbSeats.mockResolvedValue({
      count: 2,
      note: '',
      items: [
        seat({ row_uid: 'a', reason: '日涨幅偏离值达7%' }),
        seat({ row_uid: 'b', reason: '连续三个交易日涨幅偏离值达20%' }),
      ],
    })
    renderPage()
    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))

    const count = await screen.findByTestId('lhb-seat-count-buy')
    expect(count.textContent).toContain('2 条记录')
    expect(count.textContent).toContain('1 家营业部')
    expect(count.textContent).not.toContain('2 席')

    const note = screen.getByTestId('lhb-seat-rows-note')
    expect(note.textContent).toContain('不等于独立席位')
    expect(note.textContent).toContain('上榜原因')

    // 两行都要在原席位列表里(不因为合并计数而吞行)
    const buy = screen.getByTestId('lhb-seats-buy')
    expect(within(buy).getAllByText(/上榜原因/).length).toBe(2)
  })
})

describe('ETF/可转债交接参谋: 不承诺不会出现的股票锚点', () => {
  it('ETF 510300: 不带 symbol, 提示写明没被识别为锚点(不猜类型), 且是预填不自动发送', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt({ symbol: '510300', name: '沪深300ETF' })]))
    renderPage()

    fireEvent.click(await screen.findByTestId('lhb-stock-510300'))
    const hint = await screen.findByTestId('lhb-analyze-hint')
    expect(hint.textContent).toContain('预填')
    expect(hint.textContent).toContain('不会自动发送')
    expect(hint.textContent).toContain('未被参谋入口识别为股票锚点')
    expect(hint.textContent).not.toContain('非股票')

    fireEvent.click(screen.getByTestId('lhb-analyze-copilot'))
    const probe = await screen.findByTestId('copilot-probe')
    expect(probe.getAttribute('data-path')).toBe('/ai-copilot')
    const sp = probeParams()
    expect(sp.has('symbol')).toBe(false)
    expect(sp.get('q')).toContain('510300')
    expect(sp.get('q')).toContain('2026-11-06')
  })

  it('可转债 113050 同样不承诺锚点', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt({ symbol: '113050', name: '某转债' })]))
    renderPage()

    fireEvent.click(await screen.findByTestId('lhb-stock-113050'))
    const hint = await screen.findByTestId('lhb-analyze-hint')
    expect(hint.textContent).toContain('未被参谋入口识别为股票锚点')
    expect(hint.textContent).not.toContain('非股票')

    fireEvent.click(screen.getByTestId('lhb-analyze-copilot'))
    await screen.findByTestId('copilot-probe')
    const sp = probeParams()
    expect(sp.has('symbol')).toBe(false)
    expect(sp.get('q')).toContain('113050')
  })

  it('A 股 600000: 保留锚点, 文案仍明确「预填、需手动发送」', async () => {
    archiveApi.dragonTiger.mockResolvedValue(dayOk([dt()]))
    archiveApi.lhbSeats.mockResolvedValue({ count: 1, note: '', items: [seat()] })
    renderPage()

    fireEvent.click(await screen.findByTestId('lhb-stock-600000'))
    const hint = await screen.findByTestId('lhb-analyze-hint')
    expect(hint.textContent).toContain('预填')
    expect(hint.textContent).toContain('不会自动发送')
    expect(hint.textContent).toContain('锚点')
    expect(hint.textContent).toContain('600000')

    fireEvent.click(screen.getByTestId('lhb-analyze-copilot'))
    await screen.findByTestId('copilot-probe')
    expect(probeParams().get('symbol')).toBe('600000')
  })
})
