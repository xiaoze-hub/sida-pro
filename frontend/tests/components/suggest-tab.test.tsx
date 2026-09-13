// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 12 标签「建议」守五件事:
 *
 * ① **AI 建议列表真渲染, 源含盘中监测** —— 后端建议池下发的 `agent_label` 原样出现在每条建议的
 *    「来源」行(盘中监测 `intraday_monitor` / 盘后日报), 头部另给条数 + 去重后的来源清单;
 * ② **「包含过期」开关是恢复组件自带的那个开关** —— 拨动后 `/suggestions` 以
 *    `include_expired=false` 重取(证明开关真的接线, 不是装饰);
 * ③ **空态回退链** —— 无 AI 建议 + 技术指标可得 → 「技术指标基础建议」(由
 *    `useInsightDerived.technicalFallbackSuggestion` → `buildKlineSuggestion` 产出);
 *    技术指标也不可得 → 诚实空态「暂无建议」(不编造、不伪造 `--`);
 * ④ **「触发盘中监测」不动用户的自选/绑定状态**(v0.6.0 遗留③) —— 挂载时**不**额外触发
 *    (Provider 自动建议路径本用例用持仓态抑制; 断言等过其 700ms 延迟), 点击后恰好调一次
 *    `stocksApi.triggerAgent(0, 'intraday_monitor', { allow_unbound: true, symbol, market, name, … })`
 *    —— `stock_id=0 + allow_unbound` 命中后端"不落库"分支(`src/web/api/stocks.py:504-533`),
 *    故 `stocksApi.list/create/updateAgents` **一个都不许发**(这是"不动自选/绑定"的直接观测点);
 *    ⚠️ 但**不要**把它读成"什么都不写"(2026-09-14 复审 Finding 1 证伪了原先的"零写入/零副作用"措辞):
 *    提交成功后那轮 Agent 运行仍会落一条 `AgentRun`(`src/core/agent_runs.py:40`)与一条站内
 *    「任务完成」通知(`stocks.py:589-630` 的 `_notify` **无** `suppress_notify` 判断 →
 *    `notify_center.py:97-111` 恒 `db.add(Notification)`+`commit()`, 且不传 user_id ⇒ 兜底推给 owner)。
 *    本文件只守前端能观测的那三步写入, 后端那两条记录不在本用例的断言范围内。
 * ⑤ **可见 note 与新语义一致**: 「一次性触发: 不加入自选、不绑定盘中监测」—— 旧那句
 *    「未关注时会先加入自选并绑定盘中监测」必须消失(不再发生的写入不许留在 UI 上);
 *    且可见文案**不出现**内部 agent 名 `intraday_monitor`;
 * ⑥ **失败如实陈述**(不猜原因): 触发失败 → 事实性提示 + 固定一句「本次未发生自选 / 绑定写入」
 *    (本路径没有"部分成功", 因为触发前后都没有写入); busy 态禁用按钮 + 「提交中…」;
 * ⑦ **卸载后 5s 轮询停止**(Task 12 修复的既有泄漏, 新路径复用同一套句柄管理);
 * ⑧ **保留项 `handleSetAlert`(「一键设提醒」)不被顺手改坏** —— 本标签已不调它, 故用探针组件
 *    在 Provider 内直调: 仍是 list → create(加自选) → updateAgents(绑 Agent) → 用**真实 stock.id**
 *    触发(无 `allow_unbound`), 且仍回传 `SetAlertOutcome` 如实陈述部分成功。
 *
 * 真数据纪律: mock 的是**网络层**(`@panwatch/api`), 组件与 `InsightProvider`/`useInsight*`/
 * `SuggestionsTab`/`SuggestionBadge` 全走真实代码。用例统一 `hasPosition={true}` —— 让
 * Provider 的「未持仓自动建议」路径不参与(`hasHolding` 为真即早退), 从而 `triggerAgent`
 * 的调用次数只可能来自本标签的按钮。
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

import SuggestTab from '@/pages/workbench/tabs/SuggestTab'
import InsightProvider from '@/pages/workbench/InsightProvider'
import { useInsight } from '@panwatch/biz-ui/components/insight/context'

/** 建议池 fixture: 一条盘中监测(在有效期内) + 一条盘后日报(已过期)。 */
const AI_SUGGESTIONS = [
  {
    id: 101,
    action: 'buy',
    action_label: '买入',
    signal: '盘中放量上攻, 主力净额转正',
    reason: '十档买盘占优 + 大单净流入',
    should_alert: true,
    agent_name: 'intraday_monitor',
    agent_label: '盘中监测',
    created_at: '2026-09-11T14:30:00+08:00',
    is_expired: false,
  },
  {
    id: 102,
    action: 'sell',
    action_label: '卖出',
    signal: '冲高回落, 尾盘资金净流出',
    reason: '尾盘主力净额转负',
    should_alert: true,
    agent_name: 'daily_report',
    agent_label: '盘后日报',
    created_at: '2026-09-10T15:05:00+08:00',
    is_expired: true,
  },
]

/** 技术指标 fixture: 多头排列 + MACD 金叉 + RSI 偏强(持仓态 ⇒ score 5 → 「加仓」)。 */
const KLINE_SUMMARY = {
  timeframe: '1d',
  asof: '2026-09-11',
  trend: '均线多头排列',
  macd_status: 'MACD金叉',
  rsi_status: 'RSI偏强',
  recent_5_up: 0,
  change_5d: null,
  change_20d: null,
  ma5: null,
  ma10: null,
  ma20: null,
  support: null,
  resistance: null,
}

beforeEach(() => {
  localStorage.clear()
  mocks.quote.mockResolvedValue({ symbol: '002636', market: 'CN', name: '测试标的', current_price: 11.8 })
  mocks.moreInfo.mockResolvedValue({})
  mocks.darkFlowTq.mockResolvedValue({})
  mocks.klineSummary.mockResolvedValue({ summary: KLINE_SUMMARY })
  mocks.klines.mockResolvedValue({ klines: [] })
  mocks.portfolioSummary.mockResolvedValue({ accounts: [] })
  mocks.suggestions.mockResolvedValue(AI_SUGGESTIONS)
  mocks.news.mockResolvedValue([])
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
  vi.useRealTimers()
  vi.clearAllMocks()
})

function renderTab() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <SuggestTab symbol="002636" market="CN" hasPosition />
      </ToastProvider>
    </MemoryRouter>,
  )
}

const triggerButton = () => screen.getByRole('button', { name: '触发盘中监测' })

describe('Task 12 建议: AI 建议列表(源含盘中监测)', () => {
  it('建议池两条建议各自带来源; 头部给条数+来源清单; 「包含过期」开关拨动后重取', async () => {
    renderTab()

    // 逐条建议的「来源」行来自后端 agent_label 原文(盘中监测 / 盘后日报)
    await waitFor(() => expect(screen.getAllByText(/来源: 盘中监测/).length).toBeGreaterThanOrEqual(2))
    expect(screen.getByText(/来源: 盘后日报/)).toBeTruthy()
    // 徽章标签(买入 / 卖出)
    expect(screen.getByText('买入')).toBeTruthy()
    expect(screen.getByText('卖出')).toBeTruthy()
    // 头部: 条数 + 去重后的来源清单
    expect(screen.getByText('共 2 条')).toBeTruthy()
    expect(screen.getByText(/来源: 盘中监测 · 盘后日报/)).toBeTruthy()
    // 空态文案不得出现
    expect(screen.queryByText('暂无建议')).toBeNull()
    expect(screen.queryByText('当前显示技术指标基础建议')).toBeNull()

    // 「包含过期」开关(恢复组件自带)确实接在 /suggestions 的 include_expired 上
    expect(screen.getByText('包含过期')).toBeTruthy()
    const sw = screen.getByRole('switch', { name: '显示过期建议' })
    expect(mocks.suggestions).toHaveBeenLastCalledWith(
      '002636',
      expect.objectContaining({ market: 'CN', include_expired: true }),
    )
    fireEvent.click(sw)
    await waitFor(() =>
      expect(mocks.suggestions).toHaveBeenLastCalledWith(
        '002636',
        expect.objectContaining({ include_expired: false }),
      ),
    )
    expect(screen.getByText('仅有效')).toBeTruthy()
  })

  it('门控: keys=[suggestions, core] —— 不取 news/reports/fundamentals/deep', async () => {
    renderTab()
    await waitFor(() => expect(mocks.suggestions).toHaveBeenCalledTimes(1))
    expect(mocks.klineSummary).toHaveBeenCalledWith('002636', 'CN')
    // 标签外端点零调用
    expect(mocks.news).not.toHaveBeenCalled()
    expect(mocks.history).not.toHaveBeenCalled()
    expect(mocks.fundamentalsDetail).not.toHaveBeenCalled()
    expect(mocks.getLatestForStock).not.toHaveBeenCalled()
    expect(mocks.getHistoryComparison).not.toHaveBeenCalled()
  })
})

describe('Task 12 建议: 空态回退(诚实, 不编造)', () => {
  it('无 AI 建议 + 技术指标可得 → 回退技术指标基础建议(buildKlineSuggestion)', async () => {
    mocks.suggestions.mockResolvedValue([])
    renderTab()

    await waitFor(() => expect(screen.getByText('当前显示技术指标基础建议')).toBeTruthy())
    // 回退建议由 buildKlineSuggestion(持仓态: 多头+金叉+RSI偏强 = score 5)给出 ⇒ 「加仓」
    expect(screen.getByText('加仓')).toBeTruthy()
    expect(screen.queryByText('暂无建议')).toBeNull()
    expect(screen.queryByText(/正在自动生成 AI 建议/)).toBeNull()
  })

  it('无 AI 建议且技术指标不可得 → 「暂无建议」, 不含任何编造内容', async () => {
    mocks.suggestions.mockResolvedValue([])
    mocks.klineSummary.mockResolvedValue({ summary: null })
    renderTab()

    await waitFor(() => expect(screen.getByText('暂无建议')).toBeTruthy())
    expect(screen.queryByText('当前显示技术指标基础建议')).toBeNull()
  })
})

describe('遗留③ 触发盘中监测: 不动自选/绑定的一次性触发(不加自选/不绑 Agent)', () => {
  it('可见 note 如实陈述"一次性触发", 且不再声称会写入自选/绑定; 文案不泄漏内部 agent 名', async () => {
    renderTab()
    await waitFor(() => expect(mocks.suggestions).toHaveBeenCalled())

    const note = screen.getByTestId('suggest-trigger-note')
    expect(note.textContent).toBe('一次性触发: 不加入自选、不绑定盘中监测')
    // 旧 note(描述持久化副作用)必须消失 —— 不再发生的事不许留在 UI 上
    expect(note.textContent).not.toContain('未关注时会先加入自选')
    expect(screen.queryByText(/未关注时会先加入自选并绑定盘中监测/)).toBeNull()
    // 反面: 不只靠 title tooltip 承载语义(点击/触摸用户看不到 tooltip)
    const title = triggerButton().getAttribute('title') || ''
    expect(title.length).toBeGreaterThan(0)
    expect(title).toContain('不加入自选')
    // 可见文案(含 title)不得出现内部 agent 名 `intraday_monitor`
    expect(screen.getByTestId('suggest-tab').textContent || '').not.toMatch(/intraday_monitor/)
    expect(title).not.toMatch(/intraday_monitor/)
    expect(note.textContent || '').not.toMatch(/intraday_monitor/)
  })

  it('挂载不触发; 点击后恰好一次 triggerAgent(0, …, allow_unbound) 且**不动自选/绑定**(list/create/updateAgents 一个都不发)', async () => {
    renderTab()
    await waitFor(() => expect(mocks.suggestions).toHaveBeenCalled())
    // 等过 Provider 自动建议的 700ms 延迟(持仓态抑制 ⇒ 自动路径不参与), 证明"沉默"可证
    await act(async () => { await new Promise((r) => setTimeout(r, 900)) })
    expect(mocks.triggerAgent).not.toHaveBeenCalled()

    fireEvent.click(triggerButton())

    await waitFor(() => expect(mocks.triggerAgent).toHaveBeenCalledTimes(1))
    const [stockId, agentName, opts] = mocks.triggerAgent.mock.calls[0] as [number, string, Record<string, unknown>]
    expect(agentName).toBe('intraday_monitor')
    // 后端"不落库"分支的入口条件: stock_id<=0 + allow_unbound=true(src/web/api/stocks.py:504-533)
    expect(stockId).toBe(0)
    expect(opts).toMatchObject({
      allow_unbound: true,
      symbol: '002636',
      market: 'CN',
      name: '测试标的', // resolvedName = props.stockName || quote.name || symbol
      bypass_throttle: true,
      bypass_market_hours: true,
    })
    // **不动自选/绑定**的直接证据: 三步持久化写入(查自选/建自选/绑 Agent)一个都没发生
    expect(mocks.stocksList).not.toHaveBeenCalled()
    expect(mocks.stocksCreate).not.toHaveBeenCalled()
    expect(mocks.stocksUpdateAgents).not.toHaveBeenCalled()
    // 提交成功后立即刷新一次建议(挂载 1 次 + 触发后 1 次), 随后 5s 轮询(见卸载用例)
    await waitFor(() => expect(mocks.suggestions.mock.calls.length).toBeGreaterThan(1))
    expect(screen.queryByTestId('suggest-trigger-failed')).toBeNull()
  })

  it('触发失败: 事实性提示 + 固定陈述"本次未发生自选 / 绑定写入"(不猜原因, 不谎称已写入)', async () => {
    mocks.triggerAgent.mockRejectedValue(new Error('HTTP 500'))
    renderTab()
    await waitFor(() => expect(mocks.suggestions).toHaveBeenCalled())

    fireEvent.click(triggerButton())

    await waitFor(() => expect(screen.getByTestId('suggest-trigger-failed')).toBeTruthy())
    expect(screen.getByText(/触发失败/)).toBeTruthy()
    expect(screen.getByText(/不在此处推断/)).toBeTruthy()
    expect(screen.getByText(/本次未发生自选 \/ 绑定写入/)).toBeTruthy()
    // 无前置写入路径: 失败时自选/绑定那三步也不可能已发 —— 断言确实一个都没发
    // (不许"其实写了却声称没写"; 后端的运行记录/站内通知不属前端可观测面, 不在本断言范围)
    expect(mocks.stocksCreate).not.toHaveBeenCalled()
    expect(mocks.stocksUpdateAgents).not.toHaveBeenCalled()
    // 旧的部分成功文案不得出现(本路径没有"已写入"这回事)
    expect(screen.queryByText(/已写入\(不会自动回滚\)/)).toBeNull()
    // 按钮恢复可用(alerting 复位), 可重试
    await waitFor(() => expect((triggerButton() as HTMLButtonElement).disabled).toBe(false))
  })

  it('提交中 busy 态: 按钮禁用 + 文案「提交中…」, 落定后恢复可点', async () => {
    let release: (() => void) | null = null
    mocks.triggerAgent.mockReturnValue(new Promise((r) => { release = () => r({}) }))
    renderTab()
    await waitFor(() => expect(mocks.suggestions).toHaveBeenCalled())

    fireEvent.click(triggerButton())
    // busy 期间按钮的可及名变成「提交中…」(故不能再按「触发盘中监测」查它)
    const busy = (await screen.findByRole('button', { name: '提交中…' })) as HTMLButtonElement
    expect(busy.disabled).toBe(true)

    await act(async () => { release?.() })
    await waitFor(() => expect(screen.getByRole('button', { name: '触发盘中监测' })).toBeTruthy())
    expect((screen.getByRole('button', { name: '触发盘中监测' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('卸载后一次性触发的 5s 轮询停止(与「一键设提醒」共用同一套句柄管理)', async () => {
    vi.useFakeTimers()
    const view = renderTab()
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })

    fireEvent.click(triggerButton())
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    expect(mocks.triggerAgent).toHaveBeenCalledTimes(1)

    const beforeTick = mocks.suggestions.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(5_500) })
    expect(mocks.suggestions.mock.calls.length).toBeGreaterThan(beforeTick)

    view.unmount()
    const afterUnmount = mocks.suggestions.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(mocks.suggestions.mock.calls.length).toBe(afterUnmount)
  })
})

/**
 * `handleSetAlert`(「一键设提醒」)按遗留③ 的要求**原样保留**给需要持久化语义的调用方 ——
 * 本标签已不再调它, 故用探针组件(Provider 内直接取 action)守住它没被顺手改坏:
 * 仍是 list → create(加自选) → updateAgents(绑 Agent) → triggerAgent(**真实 stock.id**),
 * 且仍回传 `SetAlertOutcome` 如实陈述"部分成功"(触发失败时前两步已落库不回滚)。
 */
describe('遗留③ 保留项: handleSetAlert 的持久化路径不变(探针直调)', () => {
  function SetAlertProbe({ onOutcome }: { onOutcome: (o: { ok: boolean; watchlistEnsured: boolean; agentBound: boolean }) => void }) {
    const { handleSetAlert } = useInsight()
    return (
      <button type="button" onClick={() => void handleSetAlert().then(onOutcome)}>
        一键设提醒
      </button>
    )
  }

  function renderProbe(onOutcome: (o: { ok: boolean; watchlistEnsured: boolean; agentBound: boolean }) => void) {
    return render(
      <MemoryRouter>
        <ToastProvider>
          <InsightProvider symbol="002636" market="CN" hasPosition keys={['suggestions', 'core']}>
            <SetAlertProbe onOutcome={onOutcome} />
          </InsightProvider>
        </ToastProvider>
      </MemoryRouter>,
    )
  }

  it('未关注 → 先 create 加自选 + updateAgents 绑定, 再用**真实 stock.id** 触发(无 allow_unbound)', async () => {
    const outcomes: { ok: boolean; watchlistEnsured: boolean; agentBound: boolean }[] = []
    renderProbe((o) => outcomes.push(o))
    // 等 core 取数落定(quote.name → resolvedName), 避免自动建议路径干扰计数
    await act(async () => { await new Promise((r) => setTimeout(r, 900)) })
    expect(mocks.triggerAgent).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '一键设提醒' }))

    await waitFor(() => expect(mocks.triggerAgent).toHaveBeenCalledTimes(1))
    const [stockId, agentName, opts] = mocks.triggerAgent.mock.calls[0] as [number, string, Record<string, unknown>]
    expect(agentName).toBe('intraday_monitor')
    expect(stockId).toBe(1) // stocksCreate 夹具的 id
    expect(opts).toMatchObject({ bypass_throttle: true, bypass_market_hours: true })
    expect(opts.allow_unbound).toBeUndefined() // 持久化路径走的是"已绑定"分支
    expect(mocks.stocksList).toHaveBeenCalledTimes(1)
    expect(mocks.stocksCreate).toHaveBeenCalledTimes(1)
    expect(mocks.stocksUpdateAgents).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(outcomes).toEqual([{ ok: true, watchlistEnsured: true, agentBound: true }]))
  })

  it('部分成功仍如实回传: create/updateAgents 已落库而 trigger 失败 → ok=false + 两个写入位为 true', async () => {
    mocks.triggerAgent.mockRejectedValue(new Error('HTTP 500'))
    const outcomes: { ok: boolean; watchlistEnsured: boolean; agentBound: boolean }[] = []
    renderProbe((o) => outcomes.push(o))
    await act(async () => { await new Promise((r) => setTimeout(r, 900)) })

    fireEvent.click(screen.getByRole('button', { name: '一键设提醒' }))

    await waitFor(() => expect(outcomes.length).toBe(1))
    expect(outcomes[0]).toEqual({ ok: false, watchlistEnsured: true, agentBound: true })
    expect(mocks.stocksCreate).toHaveBeenCalledTimes(1)
    expect(mocks.stocksUpdateAgents).toHaveBeenCalledTimes(1)
  })
})
