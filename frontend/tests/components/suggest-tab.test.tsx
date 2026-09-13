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
 * ④ **「触发盘中监测」是真实后端作业** —— 挂载时**不**额外触发(Provider 自动建议路径本用例用
 *    持仓态抑制), 点击后恰好调一次 `stocksApi.triggerAgent(..., 'intraday_monitor')`; 失败时
 *    渲染事实性失败提示(不猜原因);
 * ⑤ **卸载后手工触发的 5s 轮询停止**(Task 12 修复的既有泄漏: 原 `handleSetAlert` 的 interval
 *    只在 125s 自停定时器里清, 切标签后最长 ~2 分钟仍在打 `/suggestions`)。
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

describe('Task 12 建议: 触发盘中监测(真实后端作业)', () => {
  it('挂载不触发; 点击后恰好调一次 triggerAgent(intraday_monitor), 且先绑定后触发', async () => {
    renderTab()
    await waitFor(() => expect(mocks.suggestions).toHaveBeenCalled())
    // 挂载不额外触发(持仓态抑制了 Provider 的自动建议路径)
    await act(async () => { await new Promise((r) => setTimeout(r, 30)) })
    expect(mocks.triggerAgent).not.toHaveBeenCalled()

    fireEvent.click(triggerButton())

    await waitFor(() => expect(mocks.triggerAgent).toHaveBeenCalledTimes(1))
    const [stockId, agentName, opts] = mocks.triggerAgent.mock.calls[0] as [number, string, Record<string, unknown>]
    expect(agentName).toBe('intraday_monitor')
    expect(stockId).toBe(1) // 未关注 → 先 create(自选) 再触发(与「一键设提醒」同一动作)
    expect(opts).toMatchObject({ bypass_throttle: true, bypass_market_hours: true })
    expect(mocks.stocksUpdateAgents).toHaveBeenCalledTimes(1)
    // 提交成功后立即刷新一次建议(挂载 1 次 + 触发后 1 次), 随后 5s 轮询(见卸载用例)
    await waitFor(() => expect(mocks.suggestions.mock.calls.length).toBeGreaterThan(1))
    // 无失败提示
    expect(screen.queryByTestId('suggest-trigger-failed')).toBeNull()
  })

  it('触发失败 → 事实性失败提示 + 按钮可重试(不伪装已提交)', async () => {
    mocks.triggerAgent.mockRejectedValue(new Error('HTTP 500'))
    renderTab()
    await waitFor(() => expect(mocks.suggestions).toHaveBeenCalled())

    fireEvent.click(triggerButton())

    await waitFor(() => expect(screen.getByTestId('suggest-trigger-failed')).toBeTruthy())
    expect(screen.getByText(/触发失败/)).toBeTruthy()
    expect(screen.getByText(/不推断/)).toBeTruthy()
    // 按钮恢复可用(alerting 复位), 可重试
    await waitFor(() => expect((triggerButton() as HTMLButtonElement).disabled).toBe(false))
  })

  it('卸载后手工触发的 5s 轮询停止(修复前会打到 ~2 分钟)', async () => {
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
