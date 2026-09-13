// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 19 去重核对(spec §三 / `DATA_OWNERSHIP`): **每个数据点全站只出现一次**。
 *
 * 做法: 把工作台的**四个拥有面**(带1 `HeaderBand`、右栏 `QuickRail`、标签 `L2Tab`、
 * 标签 `FundamentalTab`)**同屏渲染**(真组件 + mock 网络层), 然后对 brief 点名的数据点
 * 数"出现次数", 断言 == 1(或 spec 明确允许的 2 处: 主力净流入 = tab.l2 + rail 速览摘要)。
 *
 * 被点名核对的数据点(brief Part B + v0.6.0 遗留⑤ 新增 ⑦):
 *  ① 涨停价        → band1 快照行 only(rail / tab 均不得出现);
 *  ② 主力净流入    → 盘口资金 tab + rail 速览摘要 = **2 处**(spec §三 #4 明确允许), 且不得有第 3 处;
 *  ③ PE/PB/股息    → band1 快照 + 基本面 tab(+ rail 精简 2-3 行, spec #5 允许) —— 核对基本面 tab
 *                    与 band1 各有一份, 且**题材/板块**不在基本面 tab;
 *  ④ 题材/板块     → rail only(基本面 tab 的 `showConcepts={false}` 生效, 概念名不得出现);
 *  ⑤ 封单成色      → 盘口资金 tab only(带1 / rail 不得出现该词);
 *  ⑥ 三指标/共振   → rail DecisionCard only(带1/两标签不得出现).
 *  ⑦ 封单额        → **band1 快照行 only**(`DATA_OWNERSHIP.seal_amount='band1.snapshot'`;
 *                    右栏「盘口速览」原有一行「封单」是重复拥有面, 已删) —— 精确 1 处 + 值也在带1。
 *
 * **"0 违规"的证据标准(本次复审加固, Finding 1)**: 每条缺席断言都**先等该拥有面自己的数据落定**,
 * 再数缺席 —— 否则断言可能在数据在途时**空过**(vacuously pass): DOM 里还没有该面, `queryByText`
 * 自然为 `null`, 但并未证明"数据到了还不出现"。同样, 每条**存在**断言都先断言"**至少 1**"(先证
 * 该点确实在屏), 再钉**精确**次数 —— 于是"点消失"(渲染回归)会失败而非因 `>=1`/缺席而静默通过。
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

/**
 * 某标签在**整屏**所有已渲染元素中的出现次数(子串匹配; `getByText` 只匹配叶子节点)。
 * 只数"叶子"承载者: 自身文本恰好等于标签(避免父子重复计数)。
 */
const countLabel = (label: string) => {
  let n = 0
  screen.queryAllByText((_, el) => el?.textContent?.trim() === label).forEach((el) => {
    if (el.children.length === 0) n += 1
  })
  return n
}

/**
 * "最深承载者"计数: 数文本**精确等于** `label` 的**最深**元素。
 * - 对 `题材 / 板块`/`PE(TTM)`/`涨停价` 这类本身即叶子的标签, 等价于 `countLabel`;
 * - 对 `封单成色`(标签 span 内嵌「近 X 分钟 · 60s 采样」子 span)这类"标签 + 角标"结构,
 *   叶子是角标(文本 ≠ 标签), 而外层 span 文本是 `封单成色近 5 分钟 · …`(≠ 标签)——
 *   故改按"含有该标签文本的最深元素"计 1。这样"数据未到(外层没渲染)"与"渲染了但不含标签"
 *   都能区分: 前者 0 且被 `expectAtLeastOne` 先拦下。
 */
function countDeepest(surface: HTMLElement, label: string): number {
  const all = Array.from(surface.querySelectorAll('*'))
  return all.filter(
    (el) => el.textContent?.includes(label) && !Array.from(el.children).some((c) => c.textContent?.includes(label)),
  ).length
}

/** 整屏"最深承载者"计数(含未在 `within` 限定的其余拥有面)。 */
function countDeepestGlobal(label: string): number {
  const all = Array.from(document.body.querySelectorAll('*'))
  return all.filter(
    (el) => el.textContent?.includes(label) && !Array.from(el.children).some((c) => c.textContent?.includes(label)),
  ).length
}

/** 某拥有面内的叶子标签次数(该面必须是具体 DOM 节点, 否则 `within` 抛错 —— 防"面消失"空过)。 */
const countIn = (surface: HTMLElement, label: string) => {
  let n = 0
  within(surface).queryAllByText((_, el) => el?.textContent?.trim() === label).forEach((el) => {
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

/**
 * 四个拥有面的 DOM 引用(每次断言前重取 —— 断言"面存在"是缺席断言不空过的**前提**)。
 * `getByTestId` 在面缺失时抛错 ⇒ 若某个拥有面整个没渲染, 用例直接红, 不会因 `queryByText` 空过。
 */
function surfaces() {
  return {
    band1: screen.getByTestId('surface-band1'),
    rail: screen.getByTestId('surface-rail'),
    l2: screen.getByTestId('surface-l2'),
    fundamental: screen.getByTestId('surface-fundamental'),
  }
}

/**
 * 逐拥有面**等其自己的数据落定**, 再交回该面 DOM —— 缺席断言在此之前绝不下结论。
 *  anchors: 每个面一个"数据已到"的可见锚点(该面唯一真源的产物), **在该面内** `findByText`
 *  (避开同标签跨面并存 —— 如 `PE(TTM)` 同时在带1 与 rail, 全局 `findByText` 会因多命中报错):
 *   - band1: `涨停价`(来自 `/stocks/{s}/l2`.more.zt_price, 带1 自包含取数);
 *   - rail:  DecisionCard 顶层标题 `数智决策`(`/decision-pioneer` 落定);
 *   - l2:    `封单成色`(L2Tab 自有端点 `/seal-quality` 落定 —— 该面最慢的一路);
 *   - fundamental: 财务行的 `PE(TTM)`(`/stocks/{s}/l2` 落定)。
 * 六个数据点的缺席断言都在这之后, 故"面还在取数 ⇒ 空过"被结构性地排除。
 */
async function awaitAllSurfacesData() {
  await within(screen.getByTestId('surface-band1')).findByText('涨停价')
  await within(screen.getByTestId('surface-rail')).findByText('数智决策')
  await within(screen.getByTestId('surface-l2')).findByText('封单成色')
  await within(screen.getByTestId('surface-fundamental')).findByText('PE(TTM)')
}

/**
 * 存在断言辅助: **先证 ≥1**(点确实在屏 —— 渲染回归时此断言先红), **再钉精确值**。
 * 返回值供调用方追加精确断言, 避免"只断言 ≥1"这种能被"点消失"静默骗过的写法(Finding 1 核心)。
 */
function expectAtLeastOne(n: number, label: string, where: string): void {
  expect(n, `${where} 应至少出现 1 次「${label}」(0 次 = 该点在该拥有面消失, 属回归)`).toBeGreaterThanOrEqual(1)
}

describe('Task 19 去重核对(六点唯一归属)', () => {
  it('① 涨停价: 只出现在带1 快照行(rail / 两标签均无「涨停价」标签)', async () => {
    renderAllSurfaces()
    await awaitAllSurfacesData()
    const s = surfaces()
    // 先证点确实在屏(带1 恰 1) —— 若带1 不再渲染「涨停价」, 这条先红, 后面的"缺席"不空过。
    expectAtLeastOne(countIn(s.band1, '涨停价'), '涨停价', '带1')
    expect(countIn(s.band1, '涨停价')).toBe(1)
    // 全屏总数(含隐藏的第 3 处): 精确 1 —— rail / l2 / fundamental 零出现。
    expect(countLabel('涨停价')).toBe(1)
    expect(countIn(s.rail, '涨停价')).toBe(0)
    expect(countIn(s.l2, '涨停价')).toBe(0)
    expect(countIn(s.fundamental, '涨停价')).toBe(0)
  })

  it('③ PE/PB/股息: 带1 快照 + 基本面标签各一份(rail 精简档 spec #5 允许 ≤3)', async () => {
    renderAllSurfaces()
    await awaitAllSurfacesData()
    const s = surfaces()
    // 基本面标签必须各有一份(去重表 #5: 基本面标签拥有 PE/PB/股息)
    expectAtLeastOne(countIn(s.fundamental, 'PE(TTM)'), 'PE(TTM)', '基本面标签')
    expect(countIn(s.fundamental, 'PE(TTM)')).toBe(1)
    expect(countIn(s.fundamental, 'PB')).toBe(1)
    expect(countIn(s.fundamental, '股息率')).toBe(1)
    // 带1 亦各有一份(快照行)
    expect(countIn(s.band1, 'PE(TTM)')).toBe(1)
    expect(countIn(s.band1, 'PB')).toBe(1)
    // 全屏总数: band1(1) + 基本面(1) + rail 精简卡(1) = 恰 3(spec #5 允许 rail 2-3 行)
    expect(countLabel('PE(TTM)')).toBe(3)
  })

  it('④ 题材/板块: 只在右栏出现(基本面标签 `showConcepts={false}` ⇒ 概念名不出现)', async () => {
    renderAllSurfaces()
    await awaitAllSurfacesData()
    const s = surfaces()
    // 先证点确实在右栏(≥1 → 精确 1), 否则后面的"基本面缺席"可能是"整卡没渲染"的假象。
    expectAtLeastOne(countIn(s.rail, '题材 / 板块'), '题材 / 板块', '右栏')
    expect(countIn(s.rail, '题材 / 板块')).toBe(1)
    // 概念名(chip = `{概念名}{类型}` 同 span, 非叶子)由 `countDeepest` 数最深承载者:
    // 右栏 ≥1; 基本面 / 带1 / L2 三面均 **0** —— 即 `showConcepts={false}` 生效 + 全屏唯右栏。
    const railConcept = countDeepest(s.rail, '消费电子概念')
    expectAtLeastOne(railConcept, '消费电子概念', '右栏')
    // 基本面标签不得出现「概念板块」段, 也不得出现概念名
    const fund = s.fundamental
    expect(within(fund).queryByText('概念板块')).toBeNull()
    expect(within(fund).queryByText('消费电子概念')).toBeNull()
    expect(within(fund).queryByText('苹果概念')).toBeNull()
    // 带1 / L2 亦不得出现概念名(全屏唯右栏: 整屏最深承载者 == 右栏次数)
    expect(countDeepest(s.band1, '消费电子概念')).toBe(0)
    expect(countDeepest(s.l2, '消费电子概念')).toBe(0)
    expect(countDeepest(fund, '消费电子概念')).toBe(0)
    expect(countDeepestGlobal('消费电子概念')).toBe(railConcept)
  })

  it('⑤ 封单成色: 只在盘口资金标签出现(带1 / 右栏均无此词)', async () => {
    renderAllSurfaces()
    await awaitAllSurfacesData()
    const s = surfaces()
    // 先证点确实在 L2 标签(≥1), 否则后面的"缺席"可能是"整块没渲染"的假象。
    // 「封单成色」在 L2 内出现 2 处(段标题「主力意图 · 封单成色」+ 封单成色小节的角标行),
    // **同一拥有面内的重复不属违规**(去重规则约束的是"跨拥有面"); 故只钉"≥1 且在 L2 内"。
    const l2Count = countDeepest(s.l2, '封单成色')
    expectAtLeastOne(l2Count, '封单成色', '盘口资金标签')
    // **带1 / 右栏 / 基本面标签三个拥有面缺席**(本次复审补上带1 —— 原用例漏了这条)
    expect(countDeepest(s.band1, '封单成色')).toBe(0)
    expect(countDeepest(s.rail, '封单成色')).toBe(0)
    expect(countDeepest(s.fundamental, '封单成色')).toBe(0)
    // 全屏最深承载者 == L2 内的次数(即"除 L2 外零出现" —— 无隐藏的第 N 处)
    expect(countDeepestGlobal('封单成色')).toBe(l2Count)
  })

  it('⑥ 三指标/共振: 只在右栏 DecisionCard(带1/两标签无「数智决策」/「共振判定」/「三指标」当前标签)', async () => {
    renderAllSurfaces()
    await awaitAllSurfacesData()
    const s = surfaces()
    // 真实标签(非 proxy 字符串): DecisionCard 的顶层标题「数智决策」+ 分段小标题「共振判定」
    expectAtLeastOne(countIn(s.rail, '数智决策'), '数智决策', '右栏')
    expect(countIn(s.rail, '数智决策')).toBe(1)
    expectAtLeastOne(countIn(s.rail, '共振判定'), '共振判定', '右栏')
    expect(countIn(s.rail, '共振判定')).toBe(1)
    // 三灯行的真实读数标签「趋势 X / 强度 X / 资金 X」——DecisionCard 是**唯一**承载者。
    // 断言存在(叶子文本以「趋势 」/「强度 」/「资金 」开头), 再断言带1/两标签零个。
    const railTrend = within(s.rail).queryAllByText((_, el) => el?.children.length === 0 && /^趋势\s/.test(el?.textContent?.trim() ?? '')).length
    expect(railTrend, '右栏应有三灯读数行「趋势 …」').toBeGreaterThanOrEqual(1)
    for (const [name, surface] of [['带1', s.band1], ['盘口资金标签', s.l2], ['基本面标签', s.fundamental]] as const) {
      expect(countIn(surface, '数智决策'), `${name} 不得出现「数智决策」`).toBe(0)
      expect(countIn(surface, '共振判定'), `${name} 不得出现「共振判定」`).toBe(0)
      expect(countIn(surface, '三指标'), `${name} 不得出现当前标签「三指标」`).toBe(0)
    }
    // 全屏精确: 「数智决策」/「共振判定」各恰 1
    expect(countLabel('数智决策')).toBe(1)
    expect(countLabel('共振判定')).toBe(1)
  })

  it('② 主力净流入: 盘口资金标签 + 右栏速览摘要 = 2 处(spec #4 允许), 不多不少', async () => {
    renderAllSurfaces()
    await awaitAllSurfacesData()
    const s = surfaces()
    // rail 速览摘要恰一行(spec #4 允许的那 1 处)
    expectAtLeastOne(countIn(s.rail, '主力净额'), '主力净额', '右栏')
    expect(countIn(s.rail, '主力净额')).toBe(1)
    // L2 成品资金拥有它 —— **精确 1**(原为 `>=1`, 无法钉住 spec 的边界; 现改为精确值)
    expectAtLeastOne(countIn(s.l2, '主力净额'), '主力净额', '盘口资金标签')
    expect(countIn(s.l2, '主力净额')).toBe(1)
    // **全屏总数精确 2**(无隐藏的第 3 处): 带1 零出现。
    expect(countLabel('主力净额')).toBe(2)
    expect(countIn(s.band1, '主力净额')).toBe(0)
    expect(countIn(s.fundamental, '主力净额')).toBe(0)
  })

  it('⑦ 封单额(遗留⑤): 只在带1 快照行出现一次, 右栏「盘口速览」的封单行已删', async () => {
    renderAllSurfaces()
    await awaitAllSurfacesData()
    const s = surfaces()
    // 先证点确实在带1(标签 + **真值**都在): 否则后面的"缺席"可能是"整格没渲染"的假象。
    expectAtLeastOne(countIn(s.band1, '封单额'), '封单额', '带1')
    expect(countIn(s.band1, '封单额')).toBe(1)
    // `/l2` 的 more.fcamo = 8.12e8 元 → 8.12亿(带1 与右栏拿到的是**同一条响应**, 故右栏也有真值可用)
    expect(within(s.band1).getByText('8.12亿')).toBeTruthy()
    // **全屏总数精确 1**(无隐藏的第 2 处)
    expect(countLabel('封单额')).toBe(1)
    // 右栏: 新标签「封单额」与旧行标签「封单」都不许存在, 真值也不许以任何形式出现
    expect(countIn(s.rail, '封单额')).toBe(0)
    expect(countIn(s.rail, '封单')).toBe(0)
    expect(within(s.rail).queryByText('8.12亿')).toBeNull()
    expect(countLabel('封单')).toBe(0)
    // 两个标签同样零出现
    expect(countIn(s.l2, '封单额')).toBe(0)
    expect(countIn(s.fundamental, '封单额')).toBe(0)
    // 反向护栏: 「封单成色」是**另一个**数据点(tab.l2 拥有, 去重表 #8)—— 本条断言不得靠抹掉它来通过
    expectAtLeastOne(countDeepest(s.l2, '封单成色'), '封单成色', '盘口资金标签')
    // 右栏保留项(去重表 #3/#4 允许的速览摘要): 主力净额 + 五档
    expect(countIn(s.rail, '主力净额')).toBe(1)
  })

  it('无"同一标签在多个拥有面重复"的漂移(快照行 cell 名在带1 唯一)', async () => {
    renderAllSurfaces()
    await awaitAllSurfacesData()
    const s = surfaces()
    // 带1 快照行的关键 cell 各自恰一份(现价/涨跌幅不在快照行, 单列于下)
    for (const label of [
      '涨停价',
      '连板',
      'PE(动)',
      'PE(TTM)',
      'PB',
      '股息率',
      '今开',
      '最高',
      '最低',
      '换手率',
      '量比',
      // 遗留⑤ 新增三格: 成交量/振幅(三类型共享) + 封单额(个股专属)
      '成交量',
      '振幅',
      '封单额',
    ]) {
      expectAtLeastOne(countIn(s.band1, label), label, '带1')
      expect(countIn(s.band1, label)).toBe(1)
    }
    // 现价/涨跌幅: `visibleSnapshotCells` 对个股**去除了这两个 cell**(Spec §一: 顶行已醒目呈现)
    // ⇒ 带1 内**没有**「现价」这个 cell 标签, 值只在顶行以大字号裸值出现(同点只此一处)。
    expect(within(s.band1).queryByText('现价')).toBeNull()
    expect(within(s.band1).queryByText('涨跌幅')).toBeNull()
    // 整体 DOM: 涨停价/连板 只出现在带1(rail / 两标签零出现)
    expect(countLabel('涨停价')).toBe(1)
    expect(countLabel('连板')).toBe(1)
  })
})
