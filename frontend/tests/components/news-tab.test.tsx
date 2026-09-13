// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 14 标签「消息」守六件事:
 *
 * ① **两段真渲染** —— 公告(东财, `/news?source=eastmoney`)与新闻(`/news`)两段各自的
 *    标题/来源/外链列表**全部来自网络层 mock 的真实响应形状**(复用恢复组件
 *    `AnnouncementsTab`/`NewsTab` 的渲染路径), 段头给出各自条数; 两段各有一个时间窗下拉
 *    (`role="combobox"` ×2, 由恢复组件自带);
 * ② **时间窗下拉真的接在取数上** —— 公告/新闻的 `hours` 分别取自各自的 localStorage 键
 *    (`stock_insight_announcement_hours`/`stock_insight_news_hours`) ⇒ 预置不同窗口值后
 *    两段的 `/news` 请求各带**自己**的 `hours`(证明两个下拉不是装饰, 且**互不串台**)。
 *    注: Radix `Select` 的候选项只在打开时挂载, jsdom 里点不开 ⇒ 这里从**状态入口**
 *    (localStorage)验证接线, 而非模拟点开下拉(仓库现有测试同样不驱动 Radix Select);
 * ③ **门控 `keys=['news','announcements']`** —— 只打这两个键的端点; core 六端点、watchlist、
 *    suggestions、reports(`/history` 仅在新闻全空时的兜底链里才会用)、deep、fundamentals、
 *    company **一个都不发**;
 * ④ **诚实空态** —— 真·空响应 ⇒ 复用组件的「暂无公告」+「暂无相关新闻」, 且段头**不**显示
 *    「共 0 条」(首拉在途与确无内容不可混为一谈); 页面无任何编造内容;
 * ⑤ **取数失败不伪装成"无内容"** —— 两个端点 reject(恢复组件的 hook 把失败静默降级为
 *    空列表)时, 屏上仍是空态, 但必须同时出现常驻说明(**与 impl 文案全文精确匹配**, 三成因并列)
 *    `列表为空时「该时间窗内确无内容 / 取数失败 / 首拉在途」在此不可区分`, **不**声称"没有内容";
 * ⑥ **换标的不串台** —— 本标签未启用 `core`, `useInsightData:555-573` 那个(被 `core` 门控的)
 *    挂载空值重置路径**不跑** ⇒ 换标的时旧标的的 `news`/`announcements` 数组不会被清空。故
 *    `NewsTab.tsx` 的标签入口把 `key={symbol}` 挂在 Provider 上(**不写行号**: 头注增删会漂)。
 *    本用例在**新标的响应悬挂不落地**的最坏窗口里换标的, 断言上一只票的文章标题**一条都不在屏上**
 *    (去掉 `key` 必失败)。
 *
 * 真数据纪律: mock 的是**网络层**(`@panwatch/api`), 组件与 `InsightProvider`/`useInsightData`/
 * 两个恢复组件全走真实代码。
 */

const mocks = vi.hoisted(() => ({
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

import NewsTab from '@/pages/workbench/tabs/NewsTab'

/** 公告 fixture(东财源: `/news?source=eastmoney` 的响应形状)。 */
const ANNOUNCEMENTS = [
  {
    source: 'eastmoney',
    source_label: '东财公告',
    title: '关于回购股份的进展公告',
    publish_time: '2026-09-11T09:30:00+08:00',
    url: 'https://example.com/a1',
  },
  {
    source: 'eastmoney',
    source_label: '东财公告',
    title: '2026年半年度报告摘要',
    publish_time: '2026-09-10T18:05:00+08:00',
    url: 'https://example.com/a2',
  },
]

/** 新闻 fixture(非东财源 ⇒ 走 `/news` 的个股/相关新闻查询)。 */
const NEWS = [
  {
    source: 'sina',
    source_label: '新浪财经',
    title: '公司获多家机构调研关注',
    publish_time: '2026-09-11T14:02:00+08:00',
    url: 'https://example.com/n1',
  },
  {
    source: 'eastmoney',
    source_label: '东方财富',
    title: '行业景气度回升带动板块走强',
    publish_time: '2026-09-11T11:20:00+08:00',
    url: 'https://example.com/n2',
  },
]

/** 按请求参数分流: 带 `source=eastmoney` 的是公告端点, 其余是新闻端点。 */
function dispatchNews(params?: Record<string, string>) {
  return Promise.resolve(params?.source === 'eastmoney' ? ANNOUNCEMENTS : NEWS)
}

beforeEach(() => {
  localStorage.clear()
  mocks.quote.mockResolvedValue({ symbol: '002636', market: 'CN', name: '测试标的', current_price: 11.8 })
  mocks.moreInfo.mockResolvedValue({})
  mocks.darkFlowTq.mockResolvedValue({})
  mocks.klineSummary.mockResolvedValue({ summary: null })
  mocks.klines.mockResolvedValue({ klines: [] })
  mocks.portfolioSummary.mockResolvedValue({ accounts: [] })
  mocks.suggestions.mockResolvedValue([])
  mocks.news.mockImplementation(dispatchNews)
  mocks.history.mockResolvedValue([])
  mocks.company.mockResolvedValue({})
  mocks.stocksList.mockResolvedValue([])
  mocks.stocksCreate.mockResolvedValue({ id: 1, symbol: '002636', market: 'CN' })
  mocks.stocksRemove.mockResolvedValue({})
  mocks.stocksUpdateAgents.mockResolvedValue({})
  mocks.triggerAgent.mockResolvedValue({})
  mocks.getLatestForStock.mockResolvedValue(null)
  mocks.getHistoryComparison.mockResolvedValue(null)
  mocks.fundamentalsDetail.mockResolvedValue({})
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

/** 宿主树(换标的用例要用 `rerender` 复用同一棵树, 只是换 `symbol`)。 */
function tabTree(symbol = '002636', market = 'CN') {
  return (
    <MemoryRouter>
      <ToastProvider>
        <NewsTab symbol={symbol} market={market} />
      </ToastProvider>
    </MemoryRouter>
  )
}

function renderTab(symbol = '002636', market = 'CN') {
  return render(tabTree(symbol, market))
}

/** `/news` 的全部调用参数(按 `source` 分流)。 */
function newsCalls() {
  return mocks.news.mock.calls.map((c) => (c[0] ?? {}) as Record<string, string>)
}
const announcementCalls = () => newsCalls().filter((p) => p.source === 'eastmoney')
const articleCalls = () => newsCalls().filter((p) => p.source !== 'eastmoney')

describe('Task 14 消息: 公告 + 新闻两段真渲染', () => {
  it('公告段渲染东财公告条目, 新闻段渲染个股新闻条目; 各带条数与时间窗下拉', async () => {
    renderTab()

    // 公告段: 标题 + 东财来源 + 外链
    const annSection = await screen.findByTestId('news-section-announcements')
    const annTitle = await within(annSection).findByText('关于回购股份的进展公告')
    expect(annTitle.closest('a')?.getAttribute('href')).toBe('https://example.com/a1')
    await within(annSection).findByText('2026年半年度报告摘要')
    expect(annSection.textContent).toContain('东财公告')

    // 新闻段: 标题 + 新浪来源 + 外链
    const newsSection = screen.getByTestId('news-section-news')
    const newsTitle = await within(newsSection).findByText('公司获多家机构调研关注')
    expect(newsTitle.closest('a')?.getAttribute('href')).toBe('https://example.com/n1')
    await within(newsSection).findByText('行业景气度回升带动板块走强')
    expect(newsSection.textContent).toContain('新浪财经')

    // 段头: 标题 + 条数(两段各 2 条)
    expect(within(annSection).getByText('公告')).toBeTruthy()
    expect(within(newsSection).getByText('新闻')).toBeTruthy()
    expect(screen.getAllByText('共 2 条')).toHaveLength(2)

    // 两段各一个时间窗下拉(恢复组件自带), 且两段的空态都不出现
    expect(screen.getAllByRole('combobox')).toHaveLength(2)
    expect(screen.queryByText('暂无公告')).toBeNull()
    expect(screen.queryByText('暂无相关新闻')).toBeNull()

    // 公告段打的是东财源, 新闻段打的是个股新闻(不带 source)
    expect(announcementCalls().length).toBeGreaterThanOrEqual(1)
    expect(articleCalls().length).toBeGreaterThanOrEqual(1)
    expect(announcementCalls()[0]).toMatchObject({ hours: '168', source: 'eastmoney', symbols: '002636' })
    expect(articleCalls()[0]).toMatchObject({ hours: '168', symbols: '002636' })
  })

  it('门控 keys=[news,announcements]: core/watchlist/suggestions/reports/deep/fundamentals 全零', async () => {
    renderTab()
    await within(await screen.findByTestId('news-section-announcements')).findByText('关于回购股份的进展公告')

    expect(mocks.quote).not.toHaveBeenCalled()
    expect(mocks.moreInfo).not.toHaveBeenCalled()
    expect(mocks.darkFlowTq).not.toHaveBeenCalled()
    expect(mocks.klineSummary).not.toHaveBeenCalled()
    expect(mocks.klines).not.toHaveBeenCalled()
    expect(mocks.portfolioSummary).not.toHaveBeenCalled()
    expect(mocks.stocksList).not.toHaveBeenCalled()
    expect(mocks.suggestions).not.toHaveBeenCalled()
    // 新闻/公告都有内容 ⇒ 兜底链(含 news_digest 历史快照)一步都不走
    expect(mocks.history).not.toHaveBeenCalled()
    expect(mocks.company).not.toHaveBeenCalled()
    expect(mocks.fundamentalsDetail).not.toHaveBeenCalled()
    expect(mocks.getLatestForStock).not.toHaveBeenCalled()
    expect(mocks.getHistoryComparison).not.toHaveBeenCalled()
    // 未启用 suggestions 键 ⇒ 不提交后端 AI 作业
    expect(mocks.triggerAgent).not.toHaveBeenCalled()
  })

  it('时间窗下拉接在自己的 hours 上且两段互不串台(公告近180天 / 新闻近6小时)', async () => {
    // 从「状态入口」(两个下拉各自绑的 localStorage 键)预置不同窗口 —— 证明两段各取各的 hours
    localStorage.setItem('stock_insight_announcement_hours', '4320')
    localStorage.setItem('stock_insight_news_hours', '6')
    renderTab()

    await within(await screen.findByTestId('news-section-announcements')).findByText('关于回购股份的进展公告')

    expect(announcementCalls()[0]).toMatchObject({ hours: '4320', source: 'eastmoney' })
    expect(articleCalls()[0]).toMatchObject({ hours: '6' })
    // 新闻段不得带上公告的窗口(反之亦然)
    expect(articleCalls().some((p) => p.hours === '4320')).toBe(false)
    expect(announcementCalls().some((p) => p.hours === '6')).toBe(false)
  })

  it('换标的时上一只票的公告/新闻一条都不得留屏(key={symbol} 强制重挂载; 新响应仍在途)', async () => {
    // 新标的的请求**悬挂不返回** —— 模拟"新响应尚未落地"的最坏窗口(正是泄漏窗口)
    const neverSettles = new Promise<never>(() => {})
    mocks.news.mockImplementation((params?: Record<string, string>) =>
      params?.symbols === '600519' ? neverSettles : dispatchNews(params),
    )

    const { rerender } = renderTab()
    // 老标的(002636)两段都已真渲染, 且各 2 条
    await within(await screen.findByTestId('news-section-announcements')).findByText('关于回购股份的进展公告')
    await within(screen.getByTestId('news-section-news')).findByText('公司获多家机构调研关注')
    expect(screen.getAllByText('共 2 条')).toHaveLength(2)

    // 换标的(symbol 变 ⇒ Provider 的 key 变 ⇒ 整棵子树重挂载)
    rerender(tabTree('600519'))

    // 老标的的四条文章必须**立刻**消失: 新响应永不落地, 它们若还在就是"画在新标的名下"
    expect(screen.queryByText('关于回购股份的进展公告')).toBeNull()
    expect(screen.queryByText('2026年半年度报告摘要')).toBeNull()
    expect(screen.queryByText('公司获多家机构调研关注')).toBeNull()
    expect(screen.queryByText('行业景气度回升带动板块走强')).toBeNull()
    // 证明是"重挂载后的空 state"(不是旧数组残留): 两段回到空态, 且不出任何条数
    expect(await screen.findByText('暂无公告')).toBeTruthy()
    expect(await screen.findByText('暂无相关新闻')).toBeTruthy()
    expect(screen.queryByText(/共 \d+ 条/)).toBeNull()
    // 新标的的请求确实发出去了(空态不是因为"根本没取数")
    expect(newsCalls().some((p) => p.symbols === '600519')).toBe(true)
  })
})

describe('Task 14 消息: 诚实空态(不编造)', () => {
  it('两端点真·空响应 ⇒ 「暂无公告」+「暂无相关新闻」, 段头不显示「共 0 条」', async () => {
    mocks.news.mockResolvedValue([])
    renderTab()

    expect(await screen.findByText('暂无公告')).toBeTruthy()
    expect(await screen.findByText('暂无相关新闻')).toBeTruthy()
    // 空态不得被读成"确无内容": 常驻说明必须在(三种成因不可区分)
    expect(screen.getByTestId('news-empty-caveat').textContent).toContain('取数失败')
    expect(screen.getByTestId('news-empty-caveat').textContent).toContain('首拉在途')
    // 首拉在途与确无内容不混同: 空列表不出条数
    expect(screen.queryByText(/共 \d+ 条/)).toBeNull()
    // 无编造条目/编造数值
    expect(screen.queryByText(/https?:\/\//)).toBeNull()
  })

  it('两端点 reject(恢复组件把失败静默降成空列表)⇒ 空态 + 明示"不可区分", 不声称没有内容', async () => {
    mocks.news.mockRejectedValue(new Error('HTTP 500'))
    mocks.history.mockRejectedValue(new Error('HTTP 500'))
    renderTab()

    expect(await screen.findByText('暂无公告')).toBeTruthy()
    expect(await screen.findByText('暂无相关新闻')).toBeTruthy()
    // 失败被降级为空列表是**既有 hook 行为**(无失败位), 且首拉在途同为 [] —— 本标签用常驻说明
    // 如实披露三种成因不可区分, 不假装"确无内容"
    const caveat = screen.getByTestId('news-empty-caveat')
    expect(caveat.textContent).toBe('列表为空时「该时间窗内确无内容 / 取数失败 / 首拉在途」在此不可区分')
    expect(screen.queryByText(/共 \d+ 条/)).toBeNull()
  })
})
