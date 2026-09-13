// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 11 标签「盘口资金」守三件事:
 *
 * ① **七个分节各有数据源真实渲染** —— 十档买卖额(双向条+形态) · L2成品资金(moreInfo) ·
 *   盘口演变事件/幽灵单(orderbookOb) · 主力意图(klineSummary.main_intent_structured)+封单成色
 *   (sealQuality) · 资金流水(klineSummary.fund_flow) · 暗盘资金TQ(darkFlowTq) · 筹码;
 * ② **缺数据走 `--` / note, 不编造** —— 盘口 `available=false` 显后端 note; moreInfo/mainIntent/
 *   fundFlow 缺字段显 `--`; 封单成色 `available=false` 显 reason; 暗盘未采显状态原文;
 * ③ **惰性门控** —— 标签自带 `InsightProvider keys={['core']}`, 只打 core 六端点 + 本标签自有的
 *   两个端点(`/orderbook-ob`、`/seal-quality`); watchlist/news/suggestions/reports 一个都不发。
 *
 * 真数据纪律: mock 的是**网络层**(`@panwatch/api`), 组件与 `InsightProvider`/`useInsight*` 走真实代码。
 */

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  quote: vi.fn(),
  moreInfo: vi.fn(),
  darkFlowTq: vi.fn(),
  klineSummary: vi.fn(),
  klines: vi.fn(),
  portfolioSummary: vi.fn(),
  suggestions: vi.fn(),
  news: vi.fn(),
  history: vi.fn(),
  company: vi.fn(),
  orderbookOb: vi.fn(),
  sealQuality: vi.fn(),
  stocksList: vi.fn(),
  stocksCreate: vi.fn(),
  stocksRemove: vi.fn(),
  stocksUpdateAgents: vi.fn(),
  triggerAgent: vi.fn(),
  getLatestForStock: vi.fn(),
  getHistoryComparison: vi.fn(),
  fundamentalsDetail: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
  insightApi: {
    quote: (...a: unknown[]) => mocks.quote(...a),
    moreInfo: (...a: unknown[]) => mocks.moreInfo(...a),
    darkFlowTq: (...a: unknown[]) => mocks.darkFlowTq(...a),
    klineSummary: (...a: unknown[]) => mocks.klineSummary(...a),
    klines: (...a: unknown[]) => mocks.klines(...a),
    portfolioSummary: (...a: unknown[]) => mocks.portfolioSummary(...a),
    suggestions: (...a: unknown[]) => mocks.suggestions(...a),
    news: (...a: unknown[]) => mocks.news(...a),
    history: (...a: unknown[]) => mocks.history(...a),
    company: (...a: unknown[]) => mocks.company(...a),
    orderbookOb: (...a: unknown[]) => mocks.orderbookOb(...a),
    sealQuality: (...a: unknown[]) => mocks.sealQuality(...a),
  },
  stocksApi: {
    list: (...a: unknown[]) => mocks.stocksList(...a),
    create: (...a: unknown[]) => mocks.stocksCreate(...a),
    remove: (...a: unknown[]) => mocks.stocksRemove(...a),
    updateAgents: (...a: unknown[]) => mocks.stocksUpdateAgents(...a),
    triggerAgent: (...a: unknown[]) => mocks.triggerAgent(...a),
  },
  tradingAgentsApi: {
    getLatestForStock: (...a: unknown[]) => mocks.getLatestForStock(...a),
    getHistoryComparison: (...a: unknown[]) => mocks.getHistoryComparison(...a),
  },
  fundamentalsApi: {
    detail: (...a: unknown[]) => mocks.fundamentalsDetail(...a),
  },
}))

import L2Tab from '@/pages/workbench/tabs/L2Tab'

/** 盘口 fixture: 买十档 1234.5678 万 / 卖十档 876.5432 万 → 买盘占比 58.5%, OB +0.792。 */
const OB_FIXTURE = {
  available: true,
  ob_series: [
    { ts: 1757570400, dt: '2026-09-11 14:30:00', ob: 0.169, label: '中性', bid_amt10: 12_000_000, ask_amt10: 8_500_000 },
    { ts: 1757570430, dt: '2026-09-11 14:30:30', ob: 0.792, label: '买压', bid_amt10: 12_345_678, ask_amt10: 8_765_432 },
  ],
  events: [
    { type: '托单', side: 'bid', price_level: 1, price: 11.5, delta_hands: 3200, duration_s: 12.5, ts: 1757570430, note: '堆单高位回落' },
    { type: '幽灵单', side: 'ask', price_level: 3, price: 11.62, delta_hands: -6400, duration_s: 1.5, ts: 1757570440, note: '' },
  ],
  ghost_ratio: 0.2543,
  note: '快照数=5 交易时段',
}

/** 主力意图 fixture(元口径; 参与度/买占比已是百分数; profit_ratio 是 0~1 比例)。 */
const MAIN_INTENT_FIXTURE = {
  direction: 'buy',
  main_net: 120_000_000,
  big_net: 80_000_000,
  mid_net: 40_000_000,
  participation: 42.5,
  buy_ratio: 53.2,
  tail_net: -3_000_000,
  phase: '中继',
  signal: '逐笔口径大单持续净流入',
  chip_peak: 11.35,
  chip_band: { low: 10.8, high: 12.1 },
  profit_ratio: 0.62,
  data_status: 'ok',
  tick_count: 12_345,
}

beforeEach(() => {
  mocks.fetchAPI.mockResolvedValue({})
  mocks.quote.mockResolvedValue({ symbol: '002636', market: 'CN', name: '测试标的', current_price: 11.8 })
  mocks.moreInfo.mockResolvedValue({
    symbol: '002636',
    market: 'CN',
    quote_time: '2026-09-11T14:30:00',
    zjl_hb: 1234.5, // 万元
    zjl: -234.6, // 万元
    total_buy_vol: 45678,
    total_sell_vol: 32100,
    cancel_buy: 1234,
    cancel_sell: 987,
    l2_tick_num: 45678,
    l2_order_num: 3210,
    raw: {},
  })
  mocks.klineSummary.mockResolvedValue({
    summary: null, // 派生层(buildKlineSuggestion)不参与本标签, 传 null 走空
    main_intent_structured: MAIN_INTENT_FIXTURE,
    fund_flow: [
      { date: '2026-09-10', ming_net: null, dark_net: -12_345_678 },
      { date: '2026-09-11', ming_net: 8_765_432, dark_net: 1_234_567 },
    ],
  })
  mocks.darkFlowTq.mockResolvedValue({
    data_status: 'complete',
    date: '2026-09-10',
    xl_net: 2345,
    large_net: -1234,
    mid_net: 567,
    small_net: -890,
    split_order_count: 456,
    avg_split_parts: 3.2,
    cancel_ratio: 0.187,
    cancel_buy_vol: 1200,
    cancel_sell_vol: 800,
    tuopan: true,
    yapan: false,
    suopan: false,
  })
  mocks.klines.mockResolvedValue({ klines: [] })
  mocks.portfolioSummary.mockResolvedValue({ accounts: [] })
  mocks.suggestions.mockResolvedValue([])
  mocks.news.mockResolvedValue([])
  mocks.history.mockResolvedValue([])
  mocks.company.mockResolvedValue({})
  mocks.stocksList.mockResolvedValue([])
  mocks.triggerAgent.mockResolvedValue({})
  mocks.getLatestForStock.mockResolvedValue(null)
  mocks.getHistoryComparison.mockResolvedValue(null)
  mocks.fundamentalsDetail.mockResolvedValue({})
  mocks.orderbookOb.mockResolvedValue(OB_FIXTURE)
  mocks.sealQuality.mockResolvedValue({
    symbol: '002636',
    n_samples: 12,
    metrics: {
      available: true,
      window_min: 5,
      cancel_rate_5m: 0.18,
      seal_quality: 0.82,
      cancel_bias_5m: 0.25,
      cancel_zscore: 1.4,
      seal_success_rate: 0.75,
      is_sealed: true,
    },
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderTab() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <L2Tab symbol="002636" market="CN" hasPosition={false} />
      </ToastProvider>
    </MemoryRouter>,
  )
}

const section = (id: string) => screen.getByTestId(`l2-section-${id}`)
/** 分节内某标签的取值单元格(与同名标签在别节出现区分开)。 */
function cellValue(scope: HTMLElement, label: string): string {
  const labelEl = within(scope).getByText(label)
  return labelEl.parentElement?.querySelector('div:last-child')?.textContent ?? ''
}

describe('Task 11 盘口资金: 七节真实渲染', () => {
  it('十档/成品资金/事件/主力意图+封单成色/资金流水/暗盘TQ/筹码 各有真数据', async () => {
    renderTab()

    // 十档买卖额(双向条 + 形态)
    await waitFor(() => expect(within(section('orderbook')).getByText('买压')).toBeTruthy())
    const obSec = section('orderbook')
    expect(within(obSec).getByText('58.5%')).toBeTruthy() // 买盘占比
    expect(within(obSec).getByText('+0.792')).toBeTruthy() // OB 失衡
    expect(within(obSec).getByText('+1234.57万')).toBeTruthy() // 买十档额
    expect(within(obSec).getByText('+876.54万')).toBeTruthy() // 卖十档额

    // L2 成品资金(moreInfo)
    const fundSec = section('l2fund')
    await waitFor(() => expect(cellValue(fundSec, '主力净额')).not.toBe(''))
    expect(cellValue(fundSec, '主力净额')).toBe('+1234.50万') // 1234.5 万元
    expect(cellValue(fundSec, '主买净额')).toBe('-234.60万')
    expect(cellValue(fundSec, '总买/总卖量')).toMatch(/45,?678 \/ 32,?100/)
    expect(cellValue(fundSec, '撤买/撤卖量')).toMatch(/1,?234 \/ 987/)
    expect(cellValue(fundSec, '逐笔成交/委托笔数')).toMatch(/45,?678 \/ 3,?210/)

    // 盘口演变事件 + 幽灵单占比
    const evSec = section('events')
    expect(within(evSec).getByText('托单')).toBeTruthy()
    expect(within(evSec).getByText('幽灵单')).toBeTruthy()
    expect(within(evSec).getByText(/幽灵单占比 25\.4%/)).toBeTruthy()
    expect(within(evSec).getByText(/堆单高位回落/)).toBeTruthy()

    // 主力意图 + 封单成色
    const intentSec = section('intent')
    expect(cellValue(intentSec, '方向')).toBe('吸筹')
    expect(cellValue(intentSec, '主力净额(逐笔)')).toBe('+1.20亿')
    expect(cellValue(intentSec, '参与度')).toBe('42.5%')
    expect(cellValue(intentSec, '主力买占比')).toBe('53.2%')
    expect(cellValue(intentSec, '尾盘净额')).toBe('-300.00万')
    expect(cellValue(intentSec, '成色')).toBe('82%')
    expect(cellValue(intentSec, '撤单率')).toBe('18.0%')
    expect(cellValue(intentSec, '撤单异动')).toBe('1.40')
    expect(cellValue(intentSec, '封板成功率')).toBe('75%')
    expect(cellValue(intentSec, '当前封板')).toBe('封住')

    // 资金流水(明盘/暗盘净额表)
    const flowSec = section('fundflow')
    expect(within(flowSec).getByText('2026-09-11')).toBeTruthy()
    expect(within(flowSec).getByText('+876.54万')).toBeTruthy() // 当日明盘
    expect(within(flowSec).getByText('+123.46万')).toBeTruthy() // 当日暗盘
    expect(within(flowSec).getByText('-1234.57万')).toBeTruthy() // 前一日暗盘; 该行明盘 null → --

    // 暗盘资金 TQ
    const tqSec = section('darktq')
    expect(cellValue(tqSec, '超大单净额')).toBe('2345')
    expect(cellValue(tqSec, '拆单委托')).toBe('456')
    expect(cellValue(tqSec, '撤单比')).toBe('18.7%')
    expect(within(tqSec).getByText('托盘')).toBeTruthy()

    // 筹码(chip_peak / chip_band / profit_ratio)
    const chipSec = section('chips')
    expect(cellValue(chipSec, '筹码峰')).toBe('11.35')
    expect(cellValue(chipSec, '成本带')).toBe('10.80 - 12.10')
    expect(cellValue(chipSec, '获利盘比例')).toBe('62%')
  })

  it('惰性门控: 本标签只打 core 六端点 + orderbookOb/sealQuality, 标签外端点零调用', async () => {
    renderTab()
    await waitFor(() => expect(mocks.orderbookOb).toHaveBeenCalledWith('002636'))
    expect(mocks.sealQuality).toHaveBeenCalledWith('002636')
    expect(mocks.quote).toHaveBeenCalledWith('002636', 'CN')
    expect(mocks.moreInfo).toHaveBeenCalledWith('002636', 'CN')
    expect(mocks.darkFlowTq).toHaveBeenCalledWith('002636', 'CN')
    expect(mocks.klineSummary).toHaveBeenCalledWith('002636', 'CN')
    // 标签外端点(自带 keys=['core'] 的门控)
    expect(mocks.stocksList).not.toHaveBeenCalled()
    expect(mocks.news).not.toHaveBeenCalled()
    expect(mocks.suggestions).not.toHaveBeenCalled()
    expect(mocks.history).not.toHaveBeenCalled()
  })
})

describe('Task 11 盘口资金: 缺数据一律 `--` / note, 不编造', () => {
  beforeEach(() => {
    mocks.orderbookOb.mockResolvedValue({
      available: false,
      ob_series: [],
      events: [],
      ghost_ratio: 0,
      note: '盘口无数据(非交易时段或无成交), 未产出 OB 序列',
    })
    mocks.moreInfo.mockResolvedValue({ symbol: '002636', market: 'CN', raw: {} })
    mocks.klineSummary.mockResolvedValue({ summary: null })
    mocks.darkFlowTq.mockResolvedValue(null)
    mocks.sealQuality.mockResolvedValue({
      symbol: '002636',
      n_samples: 1,
      metrics: { available: false, reason: '采样不足或累计字段重置(需当日≥2个间隔≥2min的有效样本)' },
    })
  })

  it('盘口 note 原文 + 全节 `--`; 封单 reason 原文; 暗盘/筹码空态', async () => {
    renderTab()

    // ① 盘口源不可用 → 后端 note 原文 + 三个指标 `--`
    const obSec = section('orderbook')
    await waitFor(() => expect(within(obSec).getByText(/盘口无数据\(非交易时段或无成交\)/)).toBeTruthy())
    expect(within(obSec).getAllByText('--').length).toBeGreaterThanOrEqual(3)

    // ② moreInfo 缺字段 → 五个单元格全 `--`(成对量显示为 "-- / --")
    const fundSec = section('l2fund')
    for (const label of ['主力净额', '主买净额', '总买/总卖量', '撤买/撤卖量', '逐笔成交/委托笔数']) {
      expect(cellValue(fundSec, label), label).toMatch(/^--( \/ --)?$/)
    }

    // ③ 事件: 无序列 → 空态文案, 幽灵单占比 `--`
    expect(within(section('events')).getByText(/盘口无数据/)).toBeTruthy()

    // ④ 主力意图缺 → 方向 `--`; 封单成色 available=false → reason 原文
    const intentSec = section('intent')
    await waitFor(() => expect(cellValue(intentSec, '方向')).toBe('--'))
    expect(within(intentSec).getByText(/采样不足或累计字段重置/)).toBeTruthy()
    expect(within(intentSec).getByText(/暂无主力意图数据/)).toBeTruthy()

    // ⑤ 资金流水空
    expect(within(section('fundflow')).getByText(/暂无资金流水/)).toBeTruthy()

    // ⑥ 暗盘 TQ 未采 → 状态原文 + `--`(撤买/撤卖量显示为 "-- / --")
    const tqSec = section('darktq')
    expect(within(tqSec).getByText(/暗盘数据状态/)).toBeTruthy()
    expect(within(tqSec).getAllByText('--').length).toBeGreaterThanOrEqual(7)

    // ⑦ 筹码缺 → `--` + 备注
    const chipSec = section('chips')
    expect(within(chipSec).getAllByText('--').length).toBeGreaterThanOrEqual(3)
    expect(within(chipSec).getByText(/暂无筹码数据/)).toBeTruthy()
  })
})
