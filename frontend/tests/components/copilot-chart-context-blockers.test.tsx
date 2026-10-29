// @vitest-environment jsdom
/**
 * 图表上下文第二轮回审阻断（2026-10-29，独立复审复现）。
 *
 * 前一轮修掉了「点清除条消失但服务端还在注入」的部分假象，但复现出 6 个仍是假话/仍会串号的路：
 *  ① 已送入会话的快照过了 TTL，chip 写「不会发送」——**服务端每轮照样注入**（TTL 只管读侧）；
 *  ② 同页面创建的会话（带快照）关掉再重开：`openConversation` 清 `sentChart`，
 *     而 `sessionConvIds` 又让你以为"本页创建所以知道"，结果提示消失、模型照收旧快照；
 *  ③ 承接 ② 再换票：`sentChart` 已被清空 → 换票检查失效 → 新问题仍进旧会话（旧区间照旧注入）；
 *  ④ 后台执行期间换票只清了 `sentChart`，`convId` 没作废、发送按钮也不拦 `backgroundPending`
 *     → 完成时回落到旧会话 / 期间再提问直接送进旧会话；
 *  ⑤ 发送前不复核当前账号：页面读到 A 的快照后换号成 B，B 的提问仍带着 A 的区间进入请求；
 *  ⑥ 用户可见分段不写标的代码（`chartContextParts` 第一条只有「个股 · CN」），
 *     屏幕上少一样东西，送出去的 prompt 里却有 —— 违反"看不见的不许送"。
 *
 * 后端事实不变（本文件不假装有撤回接口）：`initial_context` 只在建会话时落库，
 * `_build_ai_messages()` 每轮重放，没有任何 PATCH/DELETE 能改它。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  sendMessageStream: vi.fn(),
  createConversation: vi.fn(),
  listConversations: vi.fn(),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
}))

const jobsMock = vi.hoisted(() => ({
  submitCopilotRun: vi.fn(),
  getCopilotRunStatus: vi.fn(),
}))

/** 会话列表：页面用 `convMgr.conversations` 非空时优先，故测试里让它为空、走 `convs`。 */
const convMock = vi.hoisted(() => ({
  conversations: [] as { id: number; title: string; stock_symbol?: string | null; created_at: string }[],
  list: vi.fn(async () => {}),
  rename: vi.fn(async () => ({ ok: true })),
  pin: vi.fn(async () => ({ ok: true })),
  remove: vi.fn(async () => ({ ok: true })),
}))

vi.mock('@sida/api', () => ({
  chatApi: apiMock,
  // GovernanceStrip 等组件从同一包取 fetchAPI；测试里只要不真的发请求
  fetchAPI: vi.fn(async () => ({})),
}))
vi.mock('../../src/lib/copilot/jobs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/lib/copilot/jobs')>()
  return { ...actual, submitCopilotRun: jobsMock.submitCopilotRun, getCopilotRunStatus: jobsMock.getCopilotRunStatus }
})
vi.mock('../../src/lib/copilot/api', () => ({
  getCopilotMetrics: vi.fn(async () => ({ days: 7, runs: 0, ok_rate: null, avg_tools: null, p95_elapsed_ms: null, by_recipe: [] })),
  getPendingActions: vi.fn(async () => []),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),
  getCopilotPresets: vi.fn(async () => []),
  getCopilotBrief: vi.fn(async () => null),
  getCopilotMentions: vi.fn(async () => ({ stocks: [], agents: [] })),
}))
vi.mock('../../src/hooks/useConversations', () => ({ useConversations: () => convMock }))
vi.mock('../../src/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md-fallback">{children}</div>,
}))
vi.mock('../../src/components/copilot/CopilotBriefCard', () => ({
  CopilotBriefCard: () => <div data-testid="brief-card" />,
}))
// 后台轮会挂实时面板（SSE + 治理条，与本次语义无关）；只留位置，不连流
vi.mock('../../src/components/copilot/LiveRunPanel', () => ({
  LiveRunPanel: () => <div data-testid="live-run-panel" />,
}))

import AiCopilotPage from '../../src/pages/AiCopilot'
import { ChartContextChip } from '../../src/components/copilot/ChartContextChip'
import {
  buildChartContext,
  chartContextParts,
  clearChartContextStore,
  publishChartContext,
  type ChartContext,
} from '../../src/lib/copilot/chart-context'

const OWNER = 'test-user'
const OTHER_OWNER = 'another-user'

function workbenchContext(overrides: Record<string, unknown> = {}): ChartContext {
  return buildChartContext({
    owner: OWNER,
    symbol: '600111',
    instrument: 'stock',
    market: 'CN',
    period: 'd1',
    range: { from: '2026-06-01', to: '2026-10-29', bars: 58, changePct: 12.34, amplitudePct: 23.45 },
    levels: [{ price: 12.34, kind: 'support', label: '底仓成本' }],
    now: Date.now(),
    ...overrides,
  }) as ChartContext
}

function renderPage(entry: { id: number; symbol: string; market?: string; pageContext?: string } | null) {
  return render(<AiCopilotPage entry={entry} />)
}

function token(sub: string) {
  return `e30.${btoa(JSON.stringify({ sub }))}.signature`
}

function typeQuestion(text: string) {
  fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: text } })
}

async function ask(question: string) {
  await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
  typeQuestion(question)
  fireEvent.click(screen.getByLabelText('发送'))
}

function createdPayloads() {
  return apiMock.createConversation.mock.calls.map((c) => c[0] as { stock_symbol?: string; initial_context?: string })
}

/** 后台轮会挂 LiveRunPanel（SSE）；jsdom 没有 EventSource，给个惰性桩（不连、不推事件）。 */
class StubEventSource {
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  close() {}
  addEventListener() {}
  removeEventListener() {}
}

/** 顶部工具条的「新对话」按钮（侧边栏空态文案里也有同样四个字，别点错）。 */
function clickNewConversation() {
  const button = screen
    .getAllByText('新对话')
    .find((el) => el.tagName === 'BUTTON' && !el.closest('[data-testid="copilot-conv-sidebar"]'))
  if (!button) throw new Error('未找到「新对话」按钮')
  fireEvent.click(button)
}

beforeEach(() => {
  ;(globalThis as unknown as { EventSource: unknown }).EventSource = StubEventSource
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  localStorage.setItem('token', token(OWNER))
  window.history.pushState({}, '', '/ai-copilot')
  sessionStorage.clear()
  clearChartContextStore()
  convMock.conversations = []
  apiMock.deleteConversation.mockReset()
  apiMock.createConversation.mockReset()
  apiMock.createConversation.mockResolvedValueOnce({ id: 21 }).mockResolvedValue({ id: 22 })
  apiMock.listConversations.mockReset()
  apiMock.listConversations.mockResolvedValue([])
  apiMock.getConversation.mockReset()
  apiMock.getConversation.mockResolvedValue({
    conversation: { id: 21, title: '带快照的会话', stock_symbol: '600111', created_at: '' },
    messages: [
      { id: 1, role: 'user', content: '先看看 600111', created_at: '' },
      { id: 2, role: 'assistant', content: '收到。', created_at: '' },
    ],
  })
  apiMock.sendMessageStream.mockReset()
  apiMock.sendMessageStream.mockImplementation(async (_id: number, _q: string, handlers: any) => {
    handlers.onDone({ id: 7, role: 'assistant', content: '收到。', created_at: '' })
    return { content: '收到。', authoritative: true, corruptedFrames: 0, errors: [] }
  })
  jobsMock.submitCopilotRun.mockReset()
  jobsMock.submitCopilotRun.mockResolvedValue({ run_id: 'run-1', status: 'running' })
  jobsMock.getCopilotRunStatus.mockReset()
})

afterEach(() => {
  cleanup()
  localStorage.removeItem('token')
  sessionStorage.clear()
  clearChartContextStore()
})

describe('① 已送入会话后过期：不能再说「不会发送」', () => {
  it('committed + expired 的 chip 必须写「仍会注入」，不许写「不会发送」', () => {
    render(<ChartContextChip ctx={workbenchContext()} committed expired onClear={() => {}} />)
    const text = screen.getByTestId('copilot-chart-context').textContent || ''
    // TTL 只拦"读侧新带一份"；会话里的快照后端每轮都重放，说过期就不发送是假话
    expect(text).not.toContain('不会发送')
    expect(text).toContain('已送入本会话')
    expect(text).toContain('每轮')
    expect(text).toContain('不可撤回')
  })

  it('未送入 + expired 仍要明确「不会发送」（不要连带把真话改掉）', () => {
    render(<ChartContextChip ctx={workbenchContext()} expired onClear={() => {}} />)
    const text = screen.getByTestId('copilot-chart-context').textContent || ''
    expect(text).toContain('已过期')
    expect(text).toContain('不会发送')
    expect(text).not.toContain('已送入本会话')
  })
})

describe('② 同页面会话重开：不能把已进入服务端的快照藏起来', () => {
  it('创建过带快照的会话 → 新对话 → 重开该会话：必须恢复「已送入」并说明仍在注入', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(21)
    expect((screen.getByTestId('copilot-chart-context').textContent || '')).toContain('已送入本会话')

    // 用户点「新对话」，再从对话列表把自己刚才那个会话点回来（同页面内重开）
    clickNewConversation()
    // 新对话还没送出任何快照：chip 退回「尚未送出」而不是消失（同一张图仍在屏幕上）
    await waitFor(() =>
      expect(screen.getByTestId('copilot-chart-context').textContent || '').toContain('尚未送出'),
    )
    // 列表里出现 21（模拟用户搜索/刷新了对话列表）
    convMock.conversations = [{ id: 21, title: '带快照的会话', stock_symbol: '600111', created_at: '' }]
    fireEvent.change(screen.getByTestId('copilot-conv-search'), { target: { value: '带快照' } })
    const item = await screen.findByTestId('copilot-conv-title-21')
    await act(async () => {
      fireEvent.click(item)
    })
    await waitFor(() => expect(apiMock.getConversation).toHaveBeenCalledWith(21))

    // 会话 21 创建时就带着 600111 的区间/价位，后端每轮都注入 —— 界面必须承认
    const chip = await screen.findByTestId('copilot-chart-context')
    const text = chip.textContent || ''
    expect(text).toContain('600111')
    expect(text).toContain('已送入本会话')
    expect(text).toContain('不可撤回')
    // 不许退化成"历史会话、无法核验"（这一段本页确实知道）
    expect(screen.queryByTestId('copilot-chart-context-history-notice')).toBeNull()
    // 也不许删掉会话、丢已完成回答
    expect(apiMock.deleteConversation).not.toHaveBeenCalled()
    expect(screen.getByText('收到。')).toBeTruthy()
  })
})

describe('③ 重开后换票：旧会话（含快照）不能再被复用', () => {
  it('重开带快照的 21 后问另一只票 → 必须新会话，不能把 000776 送进 21', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))

    clickNewConversation()
    convMock.conversations = [{ id: 21, title: '带快照的会话', stock_symbol: '600111', created_at: '' }]
    fireEvent.change(screen.getByTestId('copilot-conv-search'), { target: { value: '带快照' } })
    await act(async () => {
      fireEvent.click(await screen.findByTestId('copilot-conv-title-21'))
    })
    // 会话 21 已重新打开（它的历史回答原样在）
    await waitFor(() => expect(screen.getByText('先看看 600111')).toBeTruthy())
    expect(apiMock.getConversation).toHaveBeenCalledWith(21)

    await ask('000776 怎么样')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(2))
    expect(apiMock.sendMessageStream.mock.calls[1][0]).not.toBe(21)
    expect(apiMock.sendMessageStream.mock.calls[1][0]).toBe(22)
    const second = createdPayloads()[1]
    expect(second.stock_symbol).toBe('000776')
    expect(second.initial_context || '').not.toContain('图表上下文')
    expect(second.initial_context || '').not.toContain('2026-06-01')
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent).toContain('新开')
    expect(apiMock.deleteConversation).not.toHaveBeenCalled()
  })
})

describe('④ 后台执行 × 换票/再提问：旧会话必须作废', () => {
  it('后台换票后任务没给新会话 id：不得回落到旧会话 21', async () => {
    jobsMock.getCopilotRunStatus.mockResolvedValue({ run_id: 'run-1', status: 'ok', conversation_id: null })
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(21)

    fireEvent.click(screen.getByTestId('copilot-bg-toggle'))
    apiMock.getConversation.mockClear()
    await ask('000776 怎么样')
    await waitFor(() => expect(jobsMock.submitCopilotRun).toHaveBeenCalledTimes(1))
    // 换票的后台轮必须新开会话（不带旧快照）
    expect(jobsMock.submitCopilotRun.mock.calls[0][1]).toBeNull()

    // 后台轮拿不到会话 id 时，绝不能把视图/后续提问拉回 21（它带着 600111 的快照）
    await waitFor(() => expect(jobsMock.getCopilotRunStatus).toHaveBeenCalled(), { timeout: 4000 })
    await waitFor(
      () => {
        expect(apiMock.getConversation).not.toHaveBeenCalledWith(21)
      },
      { timeout: 4000 },
    )
    // 如实告知"没拿到会话 id"，而不是假装回到了某个会话
    await waitFor(() => expect(screen.getByText(/没拿到会话 id/)).toBeTruthy(), { timeout: 4000 })
    expect(apiMock.deleteConversation).not.toHaveBeenCalled()

    // 关掉后台再问一句：也不许进 21
    fireEvent.click(screen.getByTestId('copilot-bg-toggle'))
    typeQuestion('000776 的量能呢')
    await waitFor(() => expect((screen.getByLabelText('发送') as HTMLButtonElement).disabled).toBe(false), {
      timeout: 4000,
    })
    fireEvent.click(screen.getByLabelText('发送'))
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(2), { timeout: 4000 })
    expect(apiMock.sendMessageStream.mock.calls[1][0]).not.toBe(21)
  })

  it('后台任务未结束时再点发送：不得把新一轮送进旧会话（按钮也要拦）', async () => {
    jobsMock.getCopilotRunStatus.mockReturnValue(new Promise(() => {}))
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByTestId('copilot-bg-toggle'))
    await ask('000776 怎么样')
    await waitFor(() => expect(jobsMock.submitCopilotRun).toHaveBeenCalledTimes(1))

    typeQuestion('再问一句 600111')
    const sendBtn = screen.getByLabelText('发送') as HTMLButtonElement
    await waitFor(() => expect(sendBtn.disabled).toBe(true))
    fireEvent.click(sendBtn)
    // 后台还没结束：既不能再提交一轮，也不能把这条送进旧会话
    expect(jobsMock.submitCopilotRun).toHaveBeenCalledTimes(1)
    expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1)
  })
})

describe('⑤ 发送前复核当前账号', () => {
  it('A 的快照读到后换号成 B：B 的提问不得带 A 的区间，也不得进 A 的会话', async () => {
    publishChartContext(workbenchContext({ owner: OWNER }))
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(21)

    // 同一标签页换账号（localStorage 里的 JWT 换了主体）
    localStorage.setItem('token', token(OTHER_OWNER))
    await ask('这票还能拿吗')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(2))
    // 不许复用 A 的会话 21（后端会把 A 的快照每轮重放给 B）
    expect(apiMock.sendMessageStream.mock.calls[1][0]).not.toBe(21)
    const second = createdPayloads()[1]
    expect(second.initial_context || '').not.toContain('图表上下文')
    expect(second.initial_context || '').not.toContain('2026-06-01')
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent).toContain('账号')
  })
})

describe('⑥ 可见分段必须写标的代码', () => {
  it('chartContextParts 第一条要带代码（prompt 里的「标的：600111」不能只在提示词里）', () => {
    const parts = chartContextParts(workbenchContext())
    expect(parts.join(' | ')).toContain('600111')
    expect(parts.join(' | ')).toContain('个股')
  })

  it('chip 上看得见代码', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    const chip = await screen.findByTestId('copilot-chart-context')
    expect(chip.textContent || '').toContain('600111')
  })
})
