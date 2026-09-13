// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 10 门控(`enabledKeys`)守四件事:
 *
 * ① **`keys: ['core']` 只取 core** —— 挂载时只发带1+带2 的 6 个端点
 *   (`quote`/`moreInfo`/`darkFlowTq`/`klineSummary`/`klines`/`portfolioSummary`),
 *   其余端点(`stocksApi.list`(watchlist)、`news`、`announcements`、`suggestions`、
 *   `history`(reports)、tradingAgents(deep)、fundamentals)**一个都不发**;
 * ② **`suggestions` 未启用时自动 AI 建议不触发** —— `stocksApi.triggerAgent`(真实后端 AI 作业)
 *   在 900ms(> 700ms 触发窗口)后仍**零调用**;
 * ③ **默认路径(不传 `keys`)行为不变** —— 全量端点照旧 + 自动作业照旧触发(证明②的门是
 *   `keys` 造成的, 而非别的原因把触发链路打死了);
 * ④ **`keys: []` 全关 = 零请求, 且 20s 自动刷新 interval 根本不装**; 以及**卸载后 5s 轮询停止**
 *   (修复前该 interval 只在 125s 的 setTimeout 里清);
 * ⑤ **复审修复: `triggerAgent` 在途时卸载 ⇒ 续体不装轮询**(修复前 `await` 之后仍会
 *   `setInterval` + 立即 `loadSuggestions()`, 且没有任何东西会清它, 最长 ~125s 空转)。
 *
 * 说明: mock 的是**网络层**(`@panwatch/api`), Provider/两个 hook 走真实代码;
 * 上下文消费者用真组件 `Probe`(读 `useInsight()`), 无 Provider 时 `useInsight()` 会 throw。
 *
 * ④(`keys: []`)的「自动刷新不启动」判据用 `vi.spyOn(globalThis, 'setInterval')` 直接看 interval
 * **有没有被装**(本树里唯一的 interval 就是 `useInsightData` 的 20s 自动刷新) —— 只看
 * 「有没有发请求」是**证不出来的**: 空集门控会把 tick 里的每个任务组都过滤掉, 于是
 * interval 装了也照样零请求(这正是复审 Finding 1 逃过旧断言的原因)。
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

import { useInsight } from '@panwatch/biz-ui/components/insight/context'
import { InsightProvider, type ResourceKey } from '@/pages/workbench/InsightProvider'

/** 真消费者: 读 context 渲染标的代码 —— 无 Provider 时 `useInsight()` 直接 throw。 */
function Probe() {
  const ctx = useInsight()
  return <div data-testid="probe">{ctx.symbol}</div>
}

function Harness({ keys }: { keys?: readonly ResourceKey[] }) {
  return (
    <MemoryRouter>
      <ToastProvider>
        <InsightProvider symbol="000001" market="CN" stockName="测试标的" keys={keys}>
          <Probe />
        </InsightProvider>
      </ToastProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  // core
  mocks.quote.mockResolvedValue({ symbol: '000001', market: 'CN', name: '测试标的', current_price: 10 })
  mocks.moreInfo.mockResolvedValue({})
  mocks.darkFlowTq.mockResolvedValue({})
  mocks.klineSummary.mockResolvedValue(null) // 避免派生层对空 summary 做计算
  mocks.klines.mockResolvedValue({ klines: [] })
  mocks.portfolioSummary.mockResolvedValue({ accounts: [] }) // 未持仓 ⇒ 自动 AI 建议的资格满足
  // 下部标签
  mocks.suggestions.mockResolvedValue([])
  mocks.news.mockResolvedValue([])
  mocks.history.mockResolvedValue([])
  mocks.company.mockResolvedValue({})
  mocks.stocksList.mockResolvedValue([])
  mocks.stocksCreate.mockResolvedValue({ id: 1 })
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
  vi.useRealTimers()
})

const ALL_OTHER_ENDPOINTS = [
  ['stocksApi.list (watchlist)', mocks.stocksList],
  ['insightApi.news (news)', mocks.news],
  ['insightApi.suggestions (suggestions)', mocks.suggestions],
  ['insightApi.history (reports/news 兜底)', mocks.history],
  ['tradingAgentsApi.getLatestForStock (deep)', mocks.getLatestForStock],
  ['tradingAgentsApi.getHistoryComparison (deep)', mocks.getHistoryComparison],
  ['fundamentalsApi.detail (fundamentals)', mocks.fundamentalsDetail],
] as const

describe('Task 10 门控: keys=[\'core\'] 只取带1+带2 的端点', () => {
  it('core 6 端点各 1 次; 下部标签端点与自动 AI 作业零调用', async () => {
    render(<Harness keys={['core']} />)
    expect(await waitForProbe()).toBeTruthy()

    // ① core: 6 个端点各恰好 1 次
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(mocks.portfolioSummary).toHaveBeenCalledTimes(1))
    expect(mocks.quote).toHaveBeenLastCalledWith('000001', 'CN')
    expect(mocks.moreInfo).toHaveBeenCalledTimes(1)
    expect(mocks.darkFlowTq).toHaveBeenCalledTimes(1)
    expect(mocks.klineSummary).toHaveBeenCalledTimes(1)
    expect(mocks.klines).toHaveBeenCalledTimes(1)
    expect(mocks.portfolioSummary).toHaveBeenCalledTimes(1)

    // ② 其余端点: 一个都不发
    for (const [label, fn] of ALL_OTHER_ENDPOINTS) {
      expect(fn, label).not.toHaveBeenCalled()
    }

    // ③ 后门: 900ms(> 700ms 触发窗口)后 `suggestions` 未启用 ⇒ 绝不提交后端 AI 作业
    await act(async () => { await new Promise(r => setTimeout(r, 900)) })
    expect(mocks.triggerAgent).not.toHaveBeenCalled()
    // 断言①在等待后依然成立(自动刷新 interval 未被 900ms 触发 —— 其周期最小 10s)
    expect(mocks.news).not.toHaveBeenCalled()
    expect(mocks.suggestions).not.toHaveBeenCalled()
  })
})

describe('Task 10 门控: 默认(不传 keys)= 全开, 旧行为不变', () => {
  it('下部端点照取 + 自动 AI 作业照触发(证明上面的门是 keys 造成的)', async () => {
    render(<Harness />)
    expect(await waitForProbe()).toBeTruthy()

    // core 仍取
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(1))
    // 下部标签端点全部照取
    await waitFor(() => expect(mocks.stocksList).toHaveBeenCalled())
    expect(mocks.news).toHaveBeenCalled()
    expect(mocks.suggestions).toHaveBeenCalled()
    expect(mocks.history).toHaveBeenCalled()
    // deep / fundamentals 仍受内部 tab 约束(不动 = 预期), 故此处不断言其被调用
    // 自动 AI 作业照旧(未持仓 + holding 已加载)
    await act(async () => { await new Promise(r => setTimeout(r, 900)) })
    expect(mocks.triggerAgent).toHaveBeenCalledTimes(1)
    expect(mocks.triggerAgent.mock.calls[0][1]).toBe('intraday_monitor')
  })
})

describe('Task 10 门控: keys=[] 全关', () => {
  it('一个请求都不发, 且 20s 自动刷新 interval 根本不装', async () => {
    vi.useFakeTimers()
    // 复审 Finding 1 的硬判据: 修复前 `[]` 的签名 `''` 被 `''.split(',')` 变成 `['']`(size 1)
    // ⇒ `hasAnyResourceEnabled` 判真 ⇒ 下面这一次 20000ms 的 setInterval 会被装上。
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval')
    render(<Harness keys={[]} />)
    expect(probeEl()).toBeTruthy()

    // 推 25s(> 20s 自动刷新周期, 且远超 700ms 自动触发窗口)
    await act(async () => { await vi.advanceTimersByTimeAsync(25_000) })

    expect(mocks.quote).not.toHaveBeenCalled()
    expect(mocks.moreInfo).not.toHaveBeenCalled()
    expect(mocks.darkFlowTq).not.toHaveBeenCalled()
    expect(mocks.klineSummary).not.toHaveBeenCalled()
    expect(mocks.klines).not.toHaveBeenCalled()
    expect(mocks.portfolioSummary).not.toHaveBeenCalled()
    for (const [label, fn] of ALL_OTHER_ENDPOINTS) {
      expect(fn, label).not.toHaveBeenCalled()
    }
    expect(mocks.triggerAgent).not.toHaveBeenCalled()

    // 自动刷新 interval 根本没装(真组件树里唯一的 interval 就是它; 空集 = 全关按契约不启动)
    expect(setIntervalSpy).not.toHaveBeenCalled()
    setIntervalSpy.mockRestore()
  })
})

describe('Task 10 修复: 自动 AI 建议的 5s 轮询在卸载时停止', () => {
  it('卸载前在轮询, 卸载后不再发 /suggestions(修复前会打到 125s)', async () => {
    vi.useFakeTimers()
    const view = render(<Harness />)
    expect(probeEl()).toBeTruthy()

    // 先给挂载副作用一轮时间(holding 加载完成), 再跨过 700ms 的自动触发窗口
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    await act(async () => { await vi.advanceTimersByTimeAsync(900) })
    expect(mocks.triggerAgent).toHaveBeenCalledTimes(1)

    // 5s 轮询仍在跑
    const beforeTick = mocks.suggestions.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(5_500) })
    const afterTick = mocks.suggestions.mock.calls.length
    expect(afterTick).toBeGreaterThan(beforeTick)

    // 卸载后: 不再新增轮询调用(旧实现此处仍会每 5s 打一次)
    view.unmount()
    const afterUnmount = mocks.suggestions.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(mocks.suggestions.mock.calls.length).toBe(afterUnmount)
  })
})

describe('Task 10 复审修复: triggerAgent 在途时卸载', () => {
  it('await 期间卸载 ⇒ 续体不装轮询、不再发 /suggestions(修复前会空转到 125s)', async () => {
    vi.useFakeTimers()
    // `triggerAgent` 挂住不 resolve —— 模拟"后端 AI 作业提交请求在途"
    let releaseTrigger!: (value: unknown) => void
    mocks.triggerAgent.mockImplementation(
      () => new Promise((resolve) => { releaseTrigger = resolve }),
    )
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval')

    const view = render(<Harness />)
    expect(probeEl()).toBeTruthy()

    // 挂载副作用落地(holding 加载完成) → 跨过 700ms 触发窗口: 此刻 triggerAgent 在途
    await act(async () => { await vi.advanceTimersByTimeAsync(10) })
    await act(async () => { await vi.advanceTimersByTimeAsync(900) })
    expect(mocks.triggerAgent).toHaveBeenCalledTimes(1)
    const intervalsBeforeUnmount = setIntervalSpy.mock.calls.length

    // 卸载发生在 await 续体之前(切走标签): 卸载清理已跑过, refs 归 null
    view.unmount()
    const afterUnmount = mocks.suggestions.mock.calls.length

    // 放行 triggerAgent: 修复前续体会在此刻 setInterval(…, 5000) + 立即 loadSuggestions(),
    // 且这轮 interval 没有任何清理者 ⇒ 卸载后仍每 5s 打 /suggestions 直到 125s。
    await act(async () => { releaseTrigger({}) })
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

    expect(mocks.suggestions.mock.calls.length).toBe(afterUnmount)
    expect(setIntervalSpy.mock.calls.length).toBe(intervalsBeforeUnmount)
    setIntervalSpy.mockRestore()
  })
})

function probeEl() {
  return document.querySelector('[data-testid="probe"]')
}

// RTL 的 `findByTestId` 在假定时器下不可用, 故统一走 DOM 直查。
async function waitForProbe() {
  await waitFor(() => expect(probeEl()).toBeTruthy())
  return probeEl()
}
