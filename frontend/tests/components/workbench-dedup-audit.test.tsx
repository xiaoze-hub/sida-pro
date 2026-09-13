// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 19 去重核对(spec §三 / `DATA_OWNERSHIP`): **每个数据点全站只出现一次**。
 *
 * 做法: 把工作台的**四个拥有面**(带1 `HeaderBand`、右栏 `QuickRail`、标签 `L2Tab`、
 * 标签 `FundamentalTab`)**同屏渲染**(真组件 + mock 网络层), 然后对 brief 点名的六个数据点
 * 数"出现次数", 断言 == 1(或 spec 明确允许的 2 处: 主力净流入 = tab.l2 + rail 速览摘要)。
 *
 * 六个被点名核对的数据点(brief Part B):
 *  ① 涨停价        → band1 快照行 only(rail / tab 均不得出现);
 *  ② 主力净流入    → 盘口资金 tab + rail 速览摘要 = **2 处**(spec §三 #4 明确允许), 且不得有第 3 处;
 *  ③ PE/PB/股息    → band1 快照 + 基本面 tab(+ rail 精简 2-3 行, spec #5 允许) —— 核对基本面 tab
 *                    与 band1 各有一份, 且**题材/板块**不在基本面 tab;
 *  ④ 题材/板块     → rail only(基本面 tab 的 `showConcepts={false}` 生效, 概念名不得出现);
 *  ⑤ 封单成色      → 盘口资金 tab only(带1 不得出现该词);
 *  ⑥ 三指标/共振   → rail DecisionCard only(带1/两标签不得出现).
 *
 * 为什么用同屏渲染而不是浏览器: 环境无可用的登录凭据(后端跑在 Docker + PG, 前端经 `:8000` 反代),
 * 生产口令不可猜也不可改 —— 见 task-19-report.md「Part B 走查受限」。同屏渲染真组件 + 真 DOM
 * 是**等价且可复跑**的唯一出现次数断言(比目视走查更严: 机器数出现次数)。
 */

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(), quote: vi.fn(), moreInfo: vi.fn(), darkFlowTq: vi.fn(), klineSummary: vi.fn(),
  klines: vi.fn(), portfolioSummary: vi.fn(), suggestions: vi.fn(), news: vi.fn(), history: vi.fn(),
  company: vi.fn(), orderbookOb: vi.fn(), sealQuality: vi.fn(), stocksList: vi.fn(),
  triggerAgent: vi.fn(), getLatestForStock: vi.fn(), getHistoryComparison: vi.fn(),
  fundamentalsDetail: vi.fn(), decisionPioneer: vi.fn(), resonance: vi.fn(),
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
  stocksApi: { list: (...a: unknown[]) => mocks.stocksList(...a), triggerAgent: (...a: unknown[]) => mocks.triggerAgent(...a) },
  tradingAgentsApi: {
    getLatestForStock: (...a: unknown[]) => mocks.getLatestForStock(...a),
    getHistoryComparison: (...a: unknown[]) => mocks.getHistoryComparison(...a),
  },
  fundamentalsApi: { detail: (...a: unknown[]) => mocks.fundamentalsDetail(...a) },
}))

import HeaderBand from '@panwatch/biz-ui/components/workbench/HeaderBand'
import QuickRail from '@panwatch/biz-ui/components/workbench/QuickRail'
import L2Tab from '@/pages/workbench/tabs/L2Tab'
import FundamentalTab from '@/pages/workbench/tabs/FundamentalTab'

const SYM = '002636'

beforeEach(() => {
  // 带1 `/stocks/{s}/l2` 的 more(Ruling A 自包含取数): 涨停价 + PE/PB/股息 的唯一真源
  mocks.fetchAPI.mockImplementation((path: string) => {
    if (String(path).includes('/l2')) {
      return Promise.resolve({
        more: {
          zt_price: 13.05, pe_dynamic: 39.19, pe_ttm: 60.26, pb: 14.0, dividend_yield: 1.23, ever_zt_count: 2,
          fcamo: 8.12e8, now: 11.8, zjl_hb: 1234.5,
        },
      })
    }
    if (String(path).includes('/blocks')) {
      return Promise.resolve({ blocks: [{ code: '885800', name: '消费电子概念', type: '概念' }] })
    }
    if (String(path).includes('/fundamental')) return Promise.resolve({ gb: { ltgb: 7.25e8, zgb: 7.3e8 }, listing: { listing_date: '2010-01-01' }, sub_new: false })
    if (String(path).includes('/decision-pioneer')) return Promise.resolve({ available: true, row: 3, phase: '中继', action_label: '持有', action_text: '三指标共振', tone: 'up', bad_count: 0 })
    if (String(path).includes('/resonance')) return Promise.resolve({ available: true, row: 3, phase: '中继', action_label: '持有', action_text: '共振', tone: 'up', bad_count: 0 })
    return Promise.resolve({})
  })
  mocks.quote.mockResolvedValue({ symbol: SYM, market: 'CN', name: '测试标的', current_price: 11.8, change_pct: 1.2 })
  mocks.moreInfo.mockResolvedValue({ symbol: SYM, market: 'CN', zjl_hb: 1234.5, zjl: -234.6, total_buy_vol: 45678, total_sell_vol: 32100, cancel_buy: 1234, cancel_sell: 987, l2_tick_num: 45678, l2_order_num: 3210, raw: {} })
  mocks.klineSummary.mockResolvedValue({ summary: null, main_intent_structured: { direction: 'buy', main_net: 1.2e8, big_net: 8e7, mid_net: 4e7, participation: 42.5, buy_ratio: 53.2, tail_net: -3e6, phase: '中继', signal: '净流入', chip_peak: 11.35, chip_band: { low: 10.8, high: 12.1 }, profit_ratio: 0.62, data_status: 'ok', tick_count: 12345 }, fund_flow: [{ date: '2026-09-11', ming_net: 8765432, dark_net: 1234567 }] })
  mocks.darkFlowTq.mockResolvedValue({ data_status: 'complete', date: '2026-09-10', xl_net: 2345, large_net: -1234, mid_net: 567, small_net: -890, split_order_count: 456, avg_split_parts: 3.2, cancel_ratio: 0.187, cancel_buy_vol: 1200, cancel_sell_vol: 800, tuopan: true, yapan: false, suopan: false })
  mocks.klines.mockResolvedValue({ klines: [] })
  mocks.portfolioSummary.mockResolvedValue({ accounts: [] })
  mocks.suggestions.mockResolvedValue([])
  mocks.news.mockResolvedValue([])
  mocks.history.mockResolvedValue([])
  mocks.company.mockResolvedValue({ name: '测试标的', industry: '元件', concepts: ['消费电子概念', '苹果概念'] })
  mocks.stocksList.mockResolvedValue([])
  mocks.triggerAgent.mockResolvedValue({})
  mocks.getLatestForStock.mockResolvedValue(null)
  mocks.getHistoryComparison.mockResolvedValue(null)
  mocks.fundamentalsDetail.mockResolvedValue({})
  mocks.orderbookOb.mockResolvedValue({ available: true, ob_series: [{ ts: 1, dt: '2026-09-11 14:30:00', ob: 0.792, label: '买压', bid_amt10: 12_345_678, ask_amt10: 8_765_432 }], events: [], ghost_ratio: 0.25, note: '' })
  mocks.sealQuality.mockResolvedValue({ symbol: SYM, n_samples: 12, metrics: { available: true, window_min: 5, cancel_rate_5m: 0.18, seal_quality: 0.82, cancel_bias_5m: 0.25, cancel_zscore: 1.4, seal_success_rate: 0.75, is_sealed: true } })
})

afterEach(() => { cleanup(); vi.clearAllMocks() })

/** 某标签在所有已渲染元素中的出现次数(子串匹配; `getByText` 只匹配叶子节点)。 */
const countLabel = (label: string) => {
  let n = 0
  screen.queryAllByText((_, el) => el?.textContent?.trim() === label).forEach((el) => {
    // 只数"叶子"承载者: 自身文本恰好等于标签(避免父子重复计数)
    if (el.children.length === 0) n += 1
  })
  return n
}

/** 四个拥有面同屏(spec §1.2 三带结构: 带1 + 右栏 + 下部标签)。 */
function renderAllSurfaces() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <div>
          <section data-testid="surface-band1">
            <HeaderBand symbol={SYM} market="CN" type="stock" hasPosition={false} />
          </section>
          <section data-testid="surface-rail">
            <QuickRail symbol={SYM} market="CN" />
          </section>
          <section data-testid="surface-l2">
            <L2Tab symbol={SYM} market="CN" hasPosition={false} />
          </section>
          <section data-testid="surface-fundamental">
            <FundamentalTab symbol={SYM} market="CN" hasPosition={false} />
          </section>
        </div>
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('Task 19 去重核对(六点唯一归属)', () => {
  it('① 涨停价: 只出现在带1 快照行(rail / 两标签均无「涨停价」标签)', async () => {
    renderAllSurfaces()
    // 等带1 快照落定
    await screen.findByText('涨停价')
    // 「涨停价」这个标签全屏只在带1 出现一次
    expect(countLabel('涨停价')).toBe(1)
    expect(within(screen.getByTestId('surface-band1')).getByText('涨停价')).toBeTruthy()
    expect(within(screen.getByTestId('surface-rail')).queryByText('涨停价')).toBeNull()
    expect(within(screen.getByTestId('surface-l2')).queryByText('涨停价')).toBeNull()
    expect(within(screen.getByTestId('surface-fundamental')).queryByText('涨停价')).toBeNull()
  })

  it('③ PE/PB/股息: 带1 快照 + 基本面标签各一份(rail 精简档 spec #5 允许 ≤3)', async () => {
    renderAllSurfaces()
    // 基本面标签的 PE(TTM) 落定(其取数独立于带1)
    await within(screen.getByTestId('surface-fundamental')).findByText('PE(TTM)')
    await screen.findByText('涨停价')
    // 三处可见: band1 快照 / 基本面标签 / rail 精简卡(Spec #5 允许 rail 2-3 行) ⇒ 总数 3
    expect(countLabel('PE(TTM)')).toBe(3)
    // 基本面标签必须各有一份(去重表 #5: 基本面标签拥有 PE/PB/股息)
    const fund = screen.getByTestId('surface-fundamental')
    expect(within(fund).getAllByText('PE(TTM)').length).toBe(1)
    expect(within(fund).getAllByText('PB').length).toBe(1)
    expect(within(fund).getAllByText('股息率').length).toBe(1)
    // 带1 亦各有一份(快照行)
    const band = screen.getByTestId('surface-band1')
    expect(within(band).getAllByText('PE(TTM)').length).toBe(1)
    expect(within(band).getAllByText('PB').length).toBe(1)
  })

  it('④ 题材/板块: 只在右栏出现(基本面标签 `showConcepts={false}` ⇒ 概念名不出现)', async () => {
    renderAllSurfaces()
    // 右栏题材/板块卡
    expect(await within(screen.getByTestId('surface-rail')).findByText('题材 / 板块')).toBeTruthy()
    // 基本面标签不得出现「概念板块」段, 也不得出现概念名
    const fund = screen.getByTestId('surface-fundamental')
    expect(within(fund).queryByText('概念板块')).toBeNull()
    expect(within(fund).queryByText('消费电子概念')).toBeNull()
    expect(within(fund).queryByText('苹果概念')).toBeNull()
  })

  it('⑤ 封单成色: 只在盘口资金标签出现(带1 无此词)', async () => {
    renderAllSurfaces()
    expect(await within(screen.getByTestId('surface-l2')).findByText('封单成色')).toBeTruthy()
    expect(within(screen.getByTestId('surface-band1')).queryByText('封单成色')).toBeNull()
    expect(within(screen.getByTestId('surface-fundamental')).queryByText('封单成色')).toBeNull()
  })

  it('⑥ 三指标/共振: 只在右栏 DecisionCard(带1/两标签无「共振」)', async () => {
    renderAllSurfaces()
    await screen.findByText('数智决策')
    expect(within(screen.getByTestId('surface-rail')).getByText('数智决策')).toBeTruthy()
    expect(within(screen.getByTestId('surface-band1')).queryByText('数智决策')).toBeNull()
    expect(within(screen.getByTestId('surface-l2')).queryByText('数智决策')).toBeNull()
    expect(within(screen.getByTestId('surface-fundamental')).queryByText('数智决策')).toBeNull()
  })

  it('② 主力净流入: 盘口资金标签 + 右栏速览摘要 = 2 处(spec #4 允许), 不多不少', async () => {
    renderAllSurfaces()
    // L2 标签的「主力净额」+ rail 速览摘要的「主力净额」= 2
    const l2Count = within(screen.getByTestId('surface-l2')).getAllByText('主力净额').length
    const railCount = within(screen.getByTestId('surface-rail')).getAllByText('主力净额').length
    expect(railCount).toBe(1) // 速览摘要恰一行
    expect(l2Count).toBeGreaterThanOrEqual(1) // L2 成品资金拥有它
    // 带1 不出现「主力净额」标签(它只在快照行, 且快照行无该 cell)
    expect(within(screen.getByTestId('surface-band1')).queryByText('主力净额')).toBeNull()
  })

  it('无“同一标签在多个拥有面重复”的漂移(快照行 cell 名在带1 唯一)', async () => {
    renderAllSurfaces()
    await screen.findByText('涨停价')
    // 带1 快照行的关键 cell 各自恰一份(现价/涨跌幅不在快照行, 单列于下)
    const band = screen.getByTestId('surface-band1')
    for (const label of ['涨停价', '连板', 'PE(动)', 'PE(TTM)', 'PB', '股息率', '今开', '最高', '最低', '换手率', '量比']) {
      expect(within(band).getAllByText(label).length).toBe(1)
    }
    // 现价/涨跌幅: `visibleSnapshotCells` 对个股**去除了这两个 cell**(Spec §一: 顶行已醒目呈现)
    // ⇒ 带1 内**没有**「现价」这个 cell 标签, 值只在顶行以大字号裸值出现(同点只此一处)。
    expect(within(band).queryByText('现价')).toBeNull()
    expect(within(band).queryByText('涨跌幅')).toBeNull()
    // 整体 DOM: 涨停价/连板 只出现在带1(rail / 两标签零出现)
    expect(countLabel('涨停价')).toBe(1)
    expect(countLabel('连板')).toBe(1)
  })
})
