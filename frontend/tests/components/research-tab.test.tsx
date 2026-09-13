// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 15 标签「研究」守六件事:
 *
 * ① **两段真渲染** —— ① AI 报告: 三子页签(盘前/盘后/新闻)+ 正文(agent 标签 · 分析日 ·
 *    标题 · 买/卖标签 · Markdown 正文 · 「查看分析上下文」折叠)全部来自复用组件 `ReportsTab`;
 *    ② 深度分析: 决策卡(买入 · 置信度 · 理由)/ 历史决策 vs 实际涨跌 / 4 位分析师报告折叠 /
 *    免责声明, 全部来自复用组件 `DeepTab` + `deep-analysis`;
 * ② **子页签真的接在取数上** —— 点「盘后」后屏上换成 `daily_report` 那份的正文(证明复用组件的
 *    子页签走的是 `setReportTab` 真状态, 不是装饰); 段头给真实份数「共 2 份」;
 * ③ **深度分析入口真的跳 `/analysis/{symbol}/{date}`** —— 点「打开详情页 ↗」时 `window.open`
 *    收到的是复用组件自己算的 `/analysis/002636/2026-09-12`(日期取结果时间戳前 10 位);
 * ④ **门控 `keys=['reports','deep']`** —— 只打 `/history`(三 agent)与 TradingAgents 两个端点;
 *    core 六端点、watchlist、suggestions、news、announcements、company、fundamentals、
 *    triggerAgent **一个都不发**;
 * ⑤ **诚实空态(不编造)** —— 真·空响应 ⇒ 复用组件的「暂无报告」+「暂无深度分析报告」, 本标签
 *    另给**成因不可区分**的常驻说明(报告段三因: 确无报告/取数失败/首拉在途; 深度段两因:
 *    确无分析/取数失败); 段头**不**显示「共 0 份」; 屏上无任何编造条目/编造数值。
 *    另有**可区分**的一种: 列表非空而当前子页签的 agent 不在其中 ⇒ 给的是另一句文案
 *    (`research-reports-agent-missing`, 带真实份数), **不**与"没有报告"混同;
 * ⑥ **换标的不串台** —— 本标签未启用 `core`, `reports`/`deepResult` 的空值重置写在被 `core`
 *    门控的挂载总 effect 里(该路径不跑)⇒ 换标的时旧标的的报告/深度结论不会被清空, 且深度有
 *    `!deepLoaded` 守卫(不重挂载则新标的永不取数)。故标签入口把 `key` 挂在 Provider 上。
 *    本用例在**新标的响应悬挂不落地**的最坏窗口里换标的, 断言上一只票的报告标题/深度结论
 *    **一条都不在屏上**(去掉 `key` 必失败)。
 *
 * 真数据纪律: mock 的是**网络层**(`@panwatch/api`), 组件与 `InsightProvider`/`useInsightData`/
 * 恢复组件全走真实代码。
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
  taTrigger: vi.fn(),
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
    trigger: (...a: unknown[]) => mocks.taTrigger(...a),
    getLatestForStock: (...a: unknown[]) => mocks.getLatestForStock(...a),
    getHistoryComparison: (...a: unknown[]) => mocks.getHistoryComparison(...a),
  },
  fundamentalsApi: {
    detail: (...a: unknown[]) => mocks.fundamentalsDetail(...a),
  },
}))

import ResearchTab from '@/pages/workbench/tabs/ResearchTab'

/** `/history` 记录 fixture(响应形状与后端 history 行一致)。 */
function report(agent: string, title: string, content: string) {
  return {
    id: agent === 'premarket_outlook' ? 1 : 2,
    agent_name: agent,
    stock_symbol: '002636',
    analysis_date: agent === 'premarket_outlook' ? '2026-09-12' : '2026-09-11',
    title,
    content,
    suggestions: agent === 'premarket_outlook' ? { '002636': { action_label: '逢低关注' } } : null,
    prompt_context: agent === 'premarket_outlook' ? 'PROMPT-CTX-PREMARKET' : null,
    created_at: '2026-09-12T08:05:00',
  }
}

const PREMARKET = report('premarket_outlook', '盘前展望: 关注量能变化', '盘前正文: 关键位 11.50, 观察量能')
const DAILY = report('daily_report', '盘后日报: 缩量整理', '盘后正文: 缩量整理, 等待方向')
const NEWS_DIGEST = report('news_digest', '新闻速递: 机构调研', '新闻正文: 机构调研密集')

/** TradingAgents 深度分析结果(字段取自 `DeepAnalysisResult`)。 */
const DEEP = {
  agent_name: 'tradingagents',
  title: '深度分析结论',
  content: '深度正文: 多因子共振, 建议持有',
  timestamp: '2026-09-12T09:31:00',
  raw_data: {
    suggestion: { action: 'buy', action_label: '买入', confidence: 7.5, reason: '多因子共振' },
    cost_usd: 0.12,
    should_alert: false,
    decision: 'BUY',
    confidence: 7.5,
    debate_history: { history: '看多: 量能配合', judge_decision: '裁决: 持有' },
    risk_judgment: '低风险',
    analyst_reports: { market: '技术面报告内容', social: '情绪面报告内容', news: '', fundamentals: '' },
    final_decision: '买入',
    trader_plan: '分批建仓',
  },
}

/** 历史决策 vs 实际涨跌(字段取自 `HistoryComparisonResponse`)。 */
const DEEP_HISTORY = {
  items: [
    {
      trace_id: 't1',
      analysis_date: '2026-09-12',
      action: 'buy',
      action_label: '买入',
      confidence: 7.5,
      cost_usd: 0.12,
      price_at_analysis: 11.2,
      return_1d_pct: null,
      return_5d_pct: null,
      return_20d_pct: null,
      hit_20d: null,
    },
  ],
  stats: {
    total: 1,
    buy_count: 1,
    sell_count: 0,
    hold_count: 0,
    buy_hit_rate: null,
    sell_hit_rate: null,
    hold_hit_rate: null,
    overall_hit_rate: null,
    avg_return_20d_pct: null,
  },
}

/** 按 `agent_name` 分流: 只回该 agent 的 1 条报告(与真实 `/history?stock_symbol=X&limit=1` 同形)。 */
function dispatchHistory(params?: Record<string, unknown>) {
  const agent = String(params?.agent_name ?? '')
  const hit = [PREMARKET, DAILY, NEWS_DIGEST].find((r) => r.agent_name === agent)
  return Promise.resolve(hit ? [hit] : [])
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
  mocks.news.mockResolvedValue([])
  mocks.history.mockImplementation(dispatchHistory)
  mocks.company.mockResolvedValue({})
  mocks.stocksList.mockResolvedValue([])
  mocks.stocksCreate.mockResolvedValue({ id: 1, symbol: '002636', market: 'CN' })
  mocks.stocksRemove.mockResolvedValue({})
  mocks.stocksUpdateAgents.mockResolvedValue({})
  mocks.triggerAgent.mockResolvedValue({})
  mocks.taTrigger.mockResolvedValue({})
  mocks.getLatestForStock.mockResolvedValue(DEEP)
  mocks.getHistoryComparison.mockResolvedValue(DEEP_HISTORY)
  mocks.fundamentalsDetail.mockResolvedValue({})
  vi.spyOn(window, 'open').mockImplementation(() => null)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

/** 宿主树(换标的用例要用 `rerender` 复用同一棵树, 只是换 `symbol`)。 */
function tabTree(symbol = '002636', market = 'CN') {
  return (
    <MemoryRouter>
      <ToastProvider>
        <ResearchTab symbol={symbol} market={market} />
      </ToastProvider>
    </MemoryRouter>
  )
}

function renderTab(symbol = '002636', market = 'CN') {
  return render(tabTree(symbol, market))
}

/** `/history` 的全部调用参数。 */
function historyCalls() {
  return mocks.history.mock.calls.map((c) => (c[0] ?? {}) as Record<string, unknown>)
}

describe('Task 15 研究: AI 报告 + 深度分析两段真渲染', () => {
  it('报告三子页签 + 正文 与 深度分析正文/入口 全部真渲染, 子页签切换到盘后真换正文', async () => {
    renderTab()

    // 顶部口径条 + 两段(段头)
    expect(screen.getByText('AI 报告(盘前 / 盘后 / 新闻 三子页签 + 正文) · 深度分析(TradingAgents)')).toBeTruthy()
    const reportsSection = await screen.findByTestId('research-section-reports')
    const deepSection = screen.getByTestId('research-section-deep')

    // ① 报告: 默认子页签 = 盘前 ⇒ 正文是 premarket_outlook 那份
    const preTitle = await within(reportsSection).findByText('盘前展望: 关注量能变化')
    expect(preTitle).toBeTruthy()
    await within(reportsSection).findByText('盘前正文: 关键位 11.50, 观察量能')
    // agent 标签 + 分析日 + 买/卖标签(均来自复用组件)
    expect(reportsSection.textContent).toContain('盘前分析')
    expect(reportsSection.textContent).toContain('2026-09-12')
    expect(reportsSection.textContent).toContain('逢低关注')
    // 「查看分析上下文」折叠(prompt_context 存在才渲染)
    expect(within(reportsSection).getByText('查看分析上下文')).toBeTruthy()
    // 三子页签按钮 + 真实份数(三 agent 各 1 条)
    expect(within(reportsSection).getByRole('button', { name: '盘前' })).toBeTruthy()
    expect(within(reportsSection).getByRole('button', { name: '盘后' })).toBeTruthy()
    expect(within(reportsSection).getByRole('button', { name: '新闻' })).toBeTruthy()
    expect(reportsSection.textContent).toContain('共 3 份')

    // 子页签真的接线: 点「盘后」⇒ 换成 daily_report 那份正文, 盘前正文退场
    fireEvent.click(within(reportsSection).getByRole('button', { name: '盘后' }))
    await within(reportsSection).findByText('盘后日报: 缩量整理')
    await within(reportsSection).findByText('盘后正文: 缩量整理, 等待方向')
    expect(within(reportsSection).queryByText('盘前展望: 关注量能变化')).toBeNull()
    // 非默认子页签也真打 /history(三 agent 各 1 次; 子页签切换不重发请求)
    expect(historyCalls().map((p) => p.agent_name).sort()).toEqual([
      'daily_report',
      'news_digest',
      'premarket_outlook',
    ])

    // ② 深度分析: 决策卡 + 正文 + 免责声明 + 历史对比
    // ('买入' 在决策卡与历史对比表里各出现一次 ⇒ 用 findAll)
    expect((await within(deepSection).findAllByText('买入')).length).toBeGreaterThanOrEqual(1)
    expect(deepSection.textContent).toContain('置信度 7.5 / 10')
    expect(deepSection.textContent).toContain('多因子共振')
    await within(deepSection).findByText('深度正文: 多因子共振, 建议持有')
    expect(within(deepSection).getByRole('button', { name: /4 位分析师报告/ })).toBeTruthy()
    expect(deepSection.textContent).toContain('历史决策 vs 实际涨跌')
    expect(deepSection.textContent).toContain('2026-09-12')

    // 入口按钮: 跳 /analysis/{symbol}/{date}(日期 = 结果时间戳前 10 位)
    fireEvent.click(within(deepSection).getByRole('button', { name: /打开详情页/ }))
    expect(window.open).toHaveBeenCalledWith('/analysis/002636/2026-09-12', '_blank')
  })

  it('门控 keys=[reports,deep]: 只打 /history ×3 与 TradingAgents 两跳, 其余端点全零', async () => {
    renderTab()
    await within(await screen.findByTestId('research-section-reports')).findByText('盘前展望: 关注量能变化')
    await within(screen.getByTestId('research-section-deep')).findByText('深度正文: 多因子共振, 建议持有')

    // 本标签启用的两个端点确实发了
    expect(historyCalls().length).toBe(3)
    expect(historyCalls().every((p) => p.stock_symbol === '002636' && p.limit === 1)).toBe(true)
    expect(mocks.getLatestForStock).toHaveBeenCalledWith('002636')
    expect(mocks.getHistoryComparison).toHaveBeenCalledWith('002636', 'CN', 90)

    // 其余端点一个都不发
    expect(mocks.quote).not.toHaveBeenCalled()
    expect(mocks.moreInfo).not.toHaveBeenCalled()
    expect(mocks.darkFlowTq).not.toHaveBeenCalled()
    expect(mocks.klineSummary).not.toHaveBeenCalled()
    expect(mocks.klines).not.toHaveBeenCalled()
    expect(mocks.portfolioSummary).not.toHaveBeenCalled()
    expect(mocks.stocksList).not.toHaveBeenCalled()
    expect(mocks.suggestions).not.toHaveBeenCalled()
    expect(mocks.news).not.toHaveBeenCalled()
    expect(mocks.company).not.toHaveBeenCalled()
    expect(mocks.fundamentalsDetail).not.toHaveBeenCalled()
    // 未启用 suggestions 键 ⇒ 不提交后端 AI 作业; 深度分析不自动触发
    expect(mocks.triggerAgent).not.toHaveBeenCalled()
    expect(mocks.taTrigger).not.toHaveBeenCalled()
  })

  it('列表非空但当前子页签的 agent 缺失 ⇒ 给可区分的另一句文案(不冒充"没有报告")', async () => {
    // 只有盘后日报一份, 默认停在盘前页签 ⇒ activeReport 为空
    mocks.history.mockImplementation((params?: Record<string, unknown>) =>
      Promise.resolve(String(params?.agent_name) === 'daily_report' ? [DAILY] : []),
    )
    renderTab()

    const reportsSection = await screen.findByTestId('research-section-reports')
    const missing = await within(reportsSection).findByTestId('research-reports-agent-missing')
    // 如实带上真实份数; 且**不**出现"成因不可区分"那条(列表并不空)
    expect(missing.textContent).toContain('已取到 1 份报告')
    expect(within(reportsSection).queryByTestId('research-reports-caveat')).toBeNull()
    expect(reportsSection.textContent).toContain('共 1 份')
  })
})

describe('Task 15 研究: 诚实空态(不编造)', () => {
  it('报告与深度都真·空 ⇒ 两条空态 + 成因不可区分说明; 段头不出「共 0 份」', async () => {
    mocks.history.mockResolvedValue([])
    mocks.getLatestForStock.mockResolvedValue(null)
    mocks.getHistoryComparison.mockResolvedValue(null)
    renderTab()

    // 复用组件的空态
    expect(await screen.findByText('暂无报告')).toBeTruthy()
    expect(await screen.findByText('暂无深度分析报告')).toBeTruthy()
    // 成因不可区分: 报告段三因(与 impl 文案全文精确匹配)
    expect(screen.getByTestId('research-reports-caveat').textContent).toBe(
      '报告列表为空时「该标的三个 agent 均无已存报告 / 取数失败 / 首拉在途」在此不可区分',
    )
    // 深度段两因(且此时历史对比也没取到 ⇒ 不带"已取到 N 条")
    const deepCaveat = await screen.findByTestId('research-deep-caveat')
    expect(deepCaveat.textContent).toContain('未取到最近一次深度分析结果')
    expect(deepCaveat.textContent).toContain('「该标的尚无深度分析 / 取数失败」在此不可区分')
    expect(deepCaveat.textContent).not.toContain('历史决策对比已取到')
    // 首拉在途与确无内容不混同: 空列表不出份数
    expect(screen.queryByText(/共 \d+ 份/)).toBeNull()
    // 无编造内容(不塞示例报告/编造结论)
    expect(screen.queryByText('盘前展望: 关注量能变化')).toBeNull()
    expect(screen.queryByText('买入')).toBeNull()
  })

  it('两端点 reject ⇒ 仍走空态, 且如实说明"不可区分", 不声称没有内容', async () => {
    mocks.history.mockRejectedValue(new Error('HTTP 500'))
    mocks.getLatestForStock.mockRejectedValue(new Error('HTTP 500'))
    mocks.getHistoryComparison.mockRejectedValue(new Error('HTTP 500'))
    renderTab()

    // 失败被复用组件/hook 静默降级为空(无失败位)—— 空态不允许被读成"确无内容"
    expect(await screen.findByText('暂无报告')).toBeTruthy()
    expect(await screen.findByText('暂无深度分析报告')).toBeTruthy()
    expect(screen.getByTestId('research-reports-caveat').textContent).toContain('取数失败')
    expect(screen.getByTestId('research-deep-caveat').textContent).toContain('取数失败')
    expect(screen.queryByText(/共 \d+ 份/)).toBeNull()
  })

  it('深度: 最近一次结果缺失但历史对比取到 ⇒ 说明里如实带上历史条数(事实陈述)', async () => {
    mocks.getLatestForStock.mockResolvedValue(null)
    renderTab()

    const deepCaveat = await screen.findByTestId('research-deep-caveat')
    expect(deepCaveat.textContent).toContain('历史决策对比已取到 1 条')
    // 历史对比本体照常渲染(= 历史部分并没有丢)
    expect(screen.getByText('历史决策 vs 实际涨跌')).toBeTruthy()
  })
})

describe('Task 15 研究: 换标的不串台', () => {
  it('换标的时上一只票的报告/深度结论一条都不得留屏(新响应悬挂; key 强制重挂载)', async () => {
    // 新标的的两个端点都悬挂不返回 —— 模拟"新响应尚未落地"的最坏窗口(正是泄漏窗口)
    const neverSettles = new Promise<never>(() => {})
    mocks.history.mockImplementation((params?: Record<string, unknown>) =>
      params?.stock_symbol === '600519' ? neverSettles : dispatchHistory(params),
    )
    mocks.getLatestForStock.mockImplementation((s: string) => (s === '600519' ? neverSettles : Promise.resolve(DEEP)))
    mocks.getHistoryComparison.mockImplementation((s: string) =>
      s === '600519' ? neverSettles : Promise.resolve(DEEP_HISTORY),
    )

    const { rerender } = renderTab()
    // 老标的(002636)两段都已真渲染
    await within(await screen.findByTestId('research-section-reports')).findByText('盘前展望: 关注量能变化')
    await within(screen.getByTestId('research-section-deep')).findByText('深度正文: 多因子共振, 建议持有')
    expect(screen.getByText('共 3 份')).toBeTruthy()

    // 换标的(symbol 变 ⇒ Provider 的 key 变 ⇒ 整棵子树重挂载)
    rerender(tabTree('600519'))

    // 老标的的报告标题/正文/深度结论必须**立刻**消失: 新响应永不落地, 它们若还在就是"画在新标的名下"
    expect(screen.queryByText('盘前展望: 关注量能变化')).toBeNull()
    expect(screen.queryByText('盘后日报: 缩量整理')).toBeNull()
    expect(screen.queryByText('深度正文: 多因子共振, 建议持有')).toBeNull()
    expect(screen.queryByText('多因子共振')).toBeNull()
    expect(screen.queryByText(/共 \d+ 份/)).toBeNull()
    // 回到**新挂载后的空 state**(不是旧数组残留)
    expect(await screen.findByText('暂无报告')).toBeTruthy()
    // 新标的的请求确实发出去了(空态不是因为"根本没取数")
    expect(historyCalls().some((p) => p.stock_symbol === '600519')).toBe(true)
    expect(mocks.getLatestForStock).toHaveBeenCalledWith('600519')
  })

  it('同一代码换市场(deep 端点是市场维度)也不得留下另一市场的深度结论', async () => {
    // 换市场后的响应一律悬挂: 若 key 不含 market, `deepLoaded === true` 会让取数 effect 早退 ⇒
    // 屏上会**一直**留着上一个市场的历史对比/结论(这正是要挡的跨市场脏窗口)
    const neverSettles = new Promise<never>(() => {})
    let hang = false
    mocks.history.mockImplementation((params?: Record<string, unknown>) =>
      hang ? neverSettles : dispatchHistory(params),
    )
    mocks.getLatestForStock.mockImplementation(() => (hang ? neverSettles : Promise.resolve(DEEP)))
    mocks.getHistoryComparison.mockImplementation((_s: string, m: string) =>
      m === 'HK' ? neverSettles : Promise.resolve(DEEP_HISTORY),
    )

    const { rerender } = renderTab()
    await within(await screen.findByTestId('research-section-reports')).findByText('盘前展望: 关注量能变化')
    await within(screen.getByTestId('research-section-deep')).findByText('深度正文: 多因子共振, 建议持有')

    hang = true
    // 同代码换市场 ⇒ key 变(市场已归一化) ⇒ 整棵子树重挂载
    rerender(tabTree('002636', 'HK'))

    expect(screen.queryByText('盘前展望: 关注量能变化')).toBeNull()
    expect(screen.queryByText('深度正文: 多因子共振, 建议持有')).toBeNull()
    expect(screen.queryByText('历史决策 vs 实际涨跌')).toBeNull()
    expect(await screen.findByText('暂无报告')).toBeTruthy()
    // 新市场确实重新发了 deep 的历史对比请求(带 HK)
    expect(mocks.getHistoryComparison).toHaveBeenCalledWith('002636', 'HK', 90)
  })
})
