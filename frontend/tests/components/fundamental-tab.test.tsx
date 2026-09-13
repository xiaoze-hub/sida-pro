// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 13 标签「基本面」守七件事:
 *
 * ① **财务/股本行真渲染**(8 格): PE(动)/PE(TTM)/PB/股息率 走 `/stocks/{s}/l2` 的 `more`
 *    (与带1 `HeaderBand`、右栏 `QuickRail` 同一套字段映射), 流通股本/总股本/上市/次新 走
 *    `GET /stocks/{s}/fundamental`; 股本与右栏**同一单位映射**(`QuickRail.fmtShares` 的亿/万双档);
 * ② **缺数据走 `--`**(8 格全 `--`), **不编造**(不写 0/不写猜测值); 取数**失败**另有中性文案且
 *    **不猜原因**(与"后端真的没数据"分开陈述);
 * ③ **龙虎榜/融资融券/股东户数**由 `FundamentalsPanel` 真渲染 —— 取数只由 `keys={['fundamentals']}`
 *    这一个键门控(Task 13 复审解耦后不再需要 Provider 内部 `tab === 'fundamentals'`, 故本标签
 *    **没有** `setTab` 代置; 把 `tab` 的与条件加回去该段就恒为「暂无基本面数据」, 见
 *    `tests/components/insight-provider-gating.test.tsx` 的变异用例);
 * ④ **公司简介/基本信息**由 `CompanyTab` 真渲染(取数由新增的 `company` 键门控 —— 本标签
 *    `keys={['fundamentals','company']}`, 不再直调 `loadCompany()`); 且**不列板块** —— 概念板块
 *    chips 归右栏(去重表 #6), 故 `showConcepts={false}` 下 `概念板块` 与概念名都不出现;
 * ⑤ **加仓计算器只在 `hasPosition` 时挂载**, 未持仓既不渲染也不打 `/portfolio/summary`;
 * ⑥ **不把持仓显示成空仓**: 有 `hasPosition` 但取不到真实持仓数时给中性说明, **不代填 0**
 *    (复用组件 0 持仓会写「当前空仓 · 建仓测算」= 对持仓标的的假陈述);
 * ⑦ **惰性门控**: `keys=['fundamentals','company']` —— quote/moreInfo/darkFlowTq/klineSummary/klines/
 *    suggestions/news/history/deep 零调用; 本标签只打 4 个端点(`fundamentals-detail` + `company` +
 *    自有的 `/stocks/{s}/l2`、`/stocks/{s}/fundamental`; 持仓时 +`/portfolio/summary`)。
 *
 * 真数据纪律: mock 的是**网络层**(`@panwatch/api`), 组件与 `InsightProvider`/`useInsight*`/
 * `FundamentalsPanel`/`CompanyTab`/`AddPositionCalculator` 全走真实代码。
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

import FundamentalTab from '@/pages/workbench/tabs/FundamentalTab'

/**
 * `/stocks/{s}/l2` fixture —— 值取真形态(通达信 get_more_info 原值, 含小数位):
 * DynaPE=39.19 / StaticPE_TTM=60.26 / PB_MRQ=14 / DYRatio=1.23。
 */
const L2_FIXTURE = {
  note: null,
  more: { pe_dynamic: 39.19, pe_ttm: 60.26, pb: 14.0, dividend_yield: 1.23 },
}

/** `/stocks/{s}/fundamental` fixture: 流通股本 7.25234944 亿 / 总股本 10 亿 / 上市 2025-03-11 / 次新。 */
const FUND_FIXTURE = {
  gb: { date: '20260912', ltgb: 725234944, zgb: 1_000_000_000 },
  listing: { name: '测试股份', listing_date: '20250311' },
  sub_new: true,
  note: null,
}

/** 龙虎榜/两融/股东户数 fixture(金额=元, 比例已是百分数值)。 */
const FUNDAMENTALS_FIXTURE = {
  dragon_tiger: [
    { trade_date: '20260911', net_buy: 123456789, reason: '日涨幅偏离值达7%' },
    { trade_date: '20260904', net_buy: -50_000_000, reason: '连续三个交易日涨幅偏离值达20%' },
  ],
  margin: [
    { date: '20260912', rz_balance: 2_000_000_000, rq_balance: 30_000_000, rz_buy: 400_000_000, total_balance: 2_030_000_000 },
  ],
  shareholders: [{ report_date: '20260630', holder_num: 51234, change_num: -2300, change_ratio: -4.3 }],
  dividend: [],
  events: [],
}

/** `/quotes/{s}/company` fixture: 含 `concepts` —— 本标签**必须不渲染**该 chips(去重表 #6)。 */
const COMPANY_FIXTURE = {
  symbol: '002636',
  name: '测试股份有限公司',
  ename: 'TEST CO., LTD.',
  market_board: '深交所主板',
  list_date: '20250311',
  bscope: '芯片设计与销售',
  desc: '公司主营业务为芯片设计与销售, 产品覆盖人工智能与汽车电子。',
  concepts: '人工智能,芯片',
  note: null,
}

const HOLDING_FIXTURE = {
  accounts: [
    {
      positions: [
        { symbol: '002636', market: 'CN', quantity: 1000, cost_price: 8.5, market_value_cny: 11800, pnl: 3300 },
      ],
    },
  ],
}

beforeEach(() => {
  localStorage.clear()
  mocks.fetchAPI.mockImplementation((path: unknown) => {
    const p = String(path)
    if (p.endsWith('/l2')) return Promise.resolve(L2_FIXTURE)
    if (p.endsWith('/fundamental')) return Promise.resolve(FUND_FIXTURE)
    return Promise.reject(new Error(`unexpected path ${p}`))
  })
  mocks.quote.mockResolvedValue({})
  mocks.moreInfo.mockResolvedValue({})
  mocks.darkFlowTq.mockResolvedValue({})
  mocks.klineSummary.mockResolvedValue({ summary: null })
  mocks.klines.mockResolvedValue({ klines: [] })
  mocks.portfolioSummary.mockResolvedValue({ accounts: [] })
  mocks.suggestions.mockResolvedValue([])
  mocks.news.mockResolvedValue([])
  mocks.history.mockResolvedValue([])
  mocks.company.mockResolvedValue(COMPANY_FIXTURE)
  mocks.stocksList.mockResolvedValue([])
  mocks.fundamentalsDetail.mockResolvedValue(FUNDAMENTALS_FIXTURE)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderTab(hasPosition = false) {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <FundamentalTab symbol="002636" market="CN" hasPosition={hasPosition} />
      </ToastProvider>
    </MemoryRouter>,
  )
}

const financeCell = (key: string) => screen.getByTestId(`fundamental-finance-${key}`)

describe('Task 13 基本面: 财务/股本行(真数据, 同一套映射)', () => {
  it('PE(动)/PE(TTM)/PB/股息率 走 /l2 的 more; 股本/上市/次新 走 /stocks/{s}/fundamental', async () => {
    renderTab()

    await waitFor(() => expect(financeCell('pe_dynamic').textContent).toContain('39.19'))
    expect(financeCell('pe_dynamic').textContent).toContain('PE(动)')
    expect(financeCell('pe_ttm').textContent).toContain('60.26')
    expect(financeCell('pb').textContent).toContain('14')
    // 股息率带 %(与带1 快照行同口径)
    expect(financeCell('dividend_yield').textContent).toContain('1.23%')
    // 股本: 与右栏 QuickRail 同一单位映射(亿/万双档)
    expect(financeCell('ltgb').textContent).toContain('7.25亿')
    expect(financeCell('zgb').textContent).toContain('10.00亿')
    // 上市: 8 位数字串补横线; 次新: 后端 sub_new=true
    expect(financeCell('listing').textContent).toContain('2025-03-11')
    expect(financeCell('sub_new').textContent).toContain('次新')
    // 股本数据日期(小字, 来自 gb.date)
    expect(screen.getByText('股本 20260912')).toBeTruthy()

    // 两个 CN 端点各打一次, 且只打这两个
    const paths = mocks.fetchAPI.mock.calls.map((c) => String(c[0]))
    expect(paths.sort()).toEqual(['/stocks/002636/fundamental', '/stocks/002636/l2'])
  })

  it('字段全缺 → 8 格全 `--`(不写 0/不编造), 且无失败文案', async () => {
    mocks.fetchAPI.mockImplementation((path: unknown) =>
      String(path).endsWith('/l2') ? Promise.resolve({ more: {} }) : Promise.resolve({}),
    )
    renderTab()

    await waitFor(() => expect(mocks.fetchAPI).toHaveBeenCalledTimes(2))
    const section = screen.getByTestId('fundamental-section-finance')
    await waitFor(() => expect(within(section).getAllByText('--').length).toBe(8))
    // 未失败 ⇒ 不应出现"取数失败"字样(失败与"无数据"分开陈述)
    expect(section.textContent || '').not.toContain('取数失败')
  })

  it('取数失败 → 中性失败文案(**不猜原因**), 值仍 `--`(不回退成 0)', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('HTTP 500'))
    renderTab()

    await waitFor(() => expect(screen.getByText(/估值取数失败/)).toBeTruthy())
    expect(screen.getByText(/股本\/上市取数失败/)).toBeTruthy()
    const section = screen.getByTestId('fundamental-section-finance')
    expect(within(section).getAllByText('--').length).toBe(8)
    // 不猜原因: 不得出现本地编造的成因
    expect(section.textContent || '').not.toMatch(/非交易时段|未接|停牌|权限/)
  })

  it('非 CN 标的: 不发 CN 专有端点, 估值/股本行全 `--` + 中性说明', async () => {
    render(
      <MemoryRouter>
        <ToastProvider>
          <FundamentalTab symbol="AAPL" market="US" />
        </ToastProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText(/非 CN 标的/)).toBeTruthy())
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
    const section = screen.getByTestId('fundamental-section-finance')
    expect(within(section).getAllByText('--').length).toBe(8)
  })
})

describe('Task 13 基本面: 龙虎榜/融资融券/股东户数(复用 FundamentalsPanel)', () => {
  it('三段真渲染(键 fundamentals 只按键即取, 标签不做任何 setTab)', async () => {
    renderTab()

    // 该段取数必须真的发生 —— 它只由 `keys=['fundamentals']` 门控(Task 13 复审解耦):
    // 若把 `tab === 'fundamentals'` 的与条件加回去, 这里恒为「暂无基本面数据」(内部 tab 恒 'overview')
    await waitFor(() => expect(mocks.fundamentalsDetail).toHaveBeenCalledTimes(1))
    expect(mocks.fundamentalsDetail).toHaveBeenCalledWith('002636', 'CN')

    const section = screen.getByTestId('fundamental-section-detail')
    await waitFor(() => expect(within(section).getAllByText('龙虎榜').length).toBeGreaterThanOrEqual(1))
    expect(within(section).getAllByText('融资融券').length).toBeGreaterThanOrEqual(1)
    expect(within(section).getAllByText('股东户数').length).toBeGreaterThanOrEqual(1)
    // 真值(净买入 1.23 亿 / 上榜原因 / 两融余额 20.00 亿与合计 20.30 亿 / 户数 5.12 万户与环比)
    expect(within(section).getByText('20260911')).toBeTruthy()
    expect(within(section).getByText('+1.23亿')).toBeTruthy()
    expect(within(section).getByText('日涨幅偏离值达7%')).toBeTruthy()
    expect(within(section).getByText('20.00亿')).toBeTruthy()
    expect(within(section).getByText('20.30亿')).toBeTruthy()
    expect(within(section).getByText('5.12万')).toBeTruthy()
    expect(within(section).getByText('-4.30%')).toBeTruthy()
  })

  it('空数据 → 复用组件的「暂无基本面数据」空态(不编造内容)', async () => {
    mocks.fundamentalsDetail.mockResolvedValue({})
    renderTab()

    await waitFor(() => expect(screen.getByText('暂无基本面数据')).toBeTruthy())
  })
})

describe('Task 13 基本面: 公司简介/基本信息(复用 CompanyTab), 不列板块', () => {
  it('简介/主营/基本信息真渲染; 概念板块 chips 不出现(归右栏)', async () => {
    renderTab()

    await waitFor(() => expect(mocks.company).toHaveBeenCalledTimes(1))
    expect(mocks.company).toHaveBeenCalledWith('002636', 'CN')

    const section = screen.getByTestId('fundamental-section-company')
    await waitFor(() => expect(within(section).getByText('测试股份有限公司')).toBeTruthy())
    expect(within(section).getByText('芯片设计与销售')).toBeTruthy()
    expect(within(section).getByText(/公司主营业务为芯片设计与销售/)).toBeTruthy()
    expect(within(section).getByText('20250311')).toBeTruthy()

    // 去重表 #6: 「基本面/简介」不再重复列板块(chips 归右栏 QuickRail)
    expect(within(section).queryByText('概念板块')).toBeNull()
    expect(within(section).queryByText('人工智能')).toBeNull()
    expect(screen.getByTestId('fundamental-tab').textContent || '').not.toContain('概念板块')
  })
})

describe('Task 13 基本面: 加仓计算器(仅持仓; 且不把持仓显示成空仓)', () => {
  it('未持仓: 既不渲染计算器, 也不打 /portfolio/summary', async () => {
    renderTab(false)

    await waitFor(() => expect(mocks.fundamentalsDetail).toHaveBeenCalled())
    expect(screen.queryByTestId('fundamental-add-position')).toBeNull()
    expect(screen.queryByText('加仓测算')).toBeNull()
    expect(mocks.portfolioSummary).not.toHaveBeenCalled()
  })

  it('持仓: 挂载计算器, 并把**真实**持仓数/成本传给它(可反推目标成本)', async () => {
    mocks.portfolioSummary.mockResolvedValue(HOLDING_FIXTURE)
    renderTab(true)

    await waitFor(() => expect(mocks.portfolioSummary).toHaveBeenCalledTimes(1))
    const wrap = await screen.findByTestId('fundamental-add-position')
    await waitFor(() => expect(within(wrap).getByText('加仓测算')).toBeTruthy())

    // 展开: 「反推:目标成本」只在 currentQuantity>0 && currentCost>0 时出现 ⇒ 证明传的是真持仓
    fireEvent.click(within(wrap).getByText('加仓测算'))
    expect(within(wrap).getByText('反推:目标成本')).toBeTruthy()
    expect(screen.getByPlaceholderText('< 8.50')).toBeTruthy()
  })

  it('有 hasPosition 但取不到持仓数 → 中性说明, **不代填 0**(不写「当前空仓 · 建仓测算」)', async () => {
    renderTab(true)

    await waitFor(() => expect(mocks.portfolioSummary).toHaveBeenCalledTimes(1))
    const wrap = await screen.findByTestId('fundamental-add-position')
    await waitFor(() => expect(wrap.getAttribute('data-state')).toBe('no-holding-data'))
    expect(wrap.textContent || '').toContain('持仓汇总中无该标的')
    expect(wrap.textContent || '').toContain('不代填 0')
    expect(screen.queryByText('加仓测算')).toBeNull()
    expect(screen.queryByText(/当前空仓/)).toBeNull()
  })

  it('持仓汇总取数失败 → 说明取数失败(与"无该标的"分开), 仍不代填 0', async () => {
    mocks.portfolioSummary.mockRejectedValue(new Error('HTTP 500'))
    renderTab(true)

    const wrap = await screen.findByTestId('fundamental-add-position')
    await waitFor(() => expect(wrap.textContent || '').toContain('持仓汇总取数失败'))
    expect(wrap.getAttribute('data-state')).toBe('no-holding-data')
    expect(screen.queryByText('加仓测算')).toBeNull()
  })
})

describe('Task 13 基本面: 惰性门控(keys=[fundamentals, company])', () => {
  it('只打本标签的端点, 其余键一个都不发', async () => {
    renderTab()

    await waitFor(() => expect(mocks.fundamentalsDetail).toHaveBeenCalled())
    await waitFor(() => expect(mocks.company).toHaveBeenCalled())
    // 标签外端点零调用
    expect(mocks.quote).not.toHaveBeenCalled()
    expect(mocks.moreInfo).not.toHaveBeenCalled()
    expect(mocks.darkFlowTq).not.toHaveBeenCalled()
    expect(mocks.klineSummary).not.toHaveBeenCalled()
    expect(mocks.klines).not.toHaveBeenCalled()
    expect(mocks.suggestions).not.toHaveBeenCalled()
    expect(mocks.news).not.toHaveBeenCalled()
    expect(mocks.history).not.toHaveBeenCalled()
    expect(mocks.getLatestForStock).not.toHaveBeenCalled()
    expect(mocks.getHistoryComparison).not.toHaveBeenCalled()
    // 未持仓 ⇒ /portfolio/summary 也不发(不启用 core 键的连带代价)
    expect(mocks.portfolioSummary).not.toHaveBeenCalled()
  })
})

/**
 * Task 19 (Finding 4): 「持仓态未知」标注的**真组件**守卫。
 *
 * `stock-workbench.test.tsx` 把 `FundamentalTab` mock 掉了(守页面接线), 于是本标签里
 * `fundamental-position-unknown` 那个 span 的 JSX 若被改坏, 页面测试**照样全绿**。
 * 本文件渲染**真组件**, 直接断言可见文案: `hasPosition === undefined`(未知) → 显示
 * 「持仓态未知 · 加仓测算暂不显示」且**不渲染**加仓计算器; 已判定(`true`/`false`) → 不显示。
 */
describe('Task 19 基本面: 持仓态未知(真组件, 可见文案)', () => {
  const renderUnknown = () =>
    render(
      <MemoryRouter>
        <ToastProvider>
          {/* hasPosition 不传 / 传 undefined = 页面持仓判定在途或失败 */}
          <FundamentalTab symbol="002636" market="CN" hasPosition={undefined} />
        </ToastProvider>
      </MemoryRouter>,
    )

  it('hasPosition=undefined: 真组件渲染可见「持仓态未知 · 加仓测算暂不显示」, 且不加仓计算器', async () => {
    renderUnknown()
    // 等财务行落定(证明标签确实渲染了, 不是整块空过)
    await waitFor(() => expect(financeCell('pe_dynamic').textContent).toContain('39.19'))
    const note = screen.getByTestId('fundamental-position-unknown')
    expect(note.textContent).toContain('持仓态未知')
    expect(note.textContent).toContain('加仓测算暂不显示')
    expect(screen.getByText(/持仓态未知/)).toBeTruthy()
    // 未知 = 无真实持仓数 ⇒ 加仓计算器不渲染(与未持仓同处置, 但有可见原因)
    expect(screen.queryByTestId('fundamental-add-position')).toBeNull()
  })

  it('hasPosition=false(已判定未持仓): 真组件**不**渲染「持仓态未知」', async () => {
    renderTab(false)
    await waitFor(() => expect(financeCell('pe_dynamic').textContent).toContain('39.19'))
    expect(screen.queryByTestId('fundamental-position-unknown')).toBeNull()
    expect(screen.queryByText(/持仓态未知/)).toBeNull()
  })
})
