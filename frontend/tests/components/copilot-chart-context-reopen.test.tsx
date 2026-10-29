// @vitest-environment jsdom
/**
 * 图表上下文第三轮回审：重开失败/在飞、刷新后换票、后台换票识别、后台新会话、建会话在飞取消。
 *
 * 这一轮针对的是**复审复现出来的实质串号**（探针思路来自独立只读审查，断言写回本仓库）：
 *  ① `openConversation` 先清 `sentChart` 再异步 GET —— GET 失败或还没回来时，页面把「这个会话带过
 *     快照」的证据丢了，而 `convId` 仍是旧值，用户接着换票提问会被送进旧会话（旧区间照旧每轮注入）；
 *  ② 刷新页面后重开历史会话：本页不知道它创建时带没带过快照，换票检查却完全失效（只靠内存记账），
 *     结果新票的问题被送进「可能带着旧图快照」的老会话 —— 一句泛化提示不足以补救；
 *  ③ 后台换票识别只看正文 6 位代码：`@` 选中的票、只说名字不说代码、会话绑定标的本身不确定，
 *     这些情况都会继续复用旧会话；
 *  ④ 后台新开会话（服务端新建、本页明知没带快照）却挂「历史会话不可核验」；
 *  ⑤ 建会话在飞时用户切走：会话已落库并带着快照，本轮却不记账 —— 之后重开只看到泛化提示；
 *  ⑥ `?period=constructor` 走了 `in` 的原型链，把 `function Object()` 当周期拼进 prompt；
 *  ⑦ 切账号 / 删除当前会话时，界面上的图表上下文条没有跟着清。
 *
 * 后端事实没变：`initial_context` 只在建会话时落库，`_build_ai_messages()` 每轮重放，
 * 没有任何接口能改它。所以本文件里所有断言都只要求「不要再骗人 / 不要再串号」，
 * 不假装有撤回接口，也不宣称服务端数据被删除。
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

const convMock = vi.hoisted(() => ({
  conversations: [] as { id: number; title: string; stock_symbol?: string | null; created_at: string }[],
  list: vi.fn(async () => {}),
  rename: vi.fn(async () => ({ ok: true })),
  pin: vi.fn(async () => ({ ok: true })),
  remove: vi.fn(async (_id: number) => ({ ok: true as boolean, error: undefined as string | undefined })),
}))

const copilotMock = vi.hoisted(() => ({
  getCopilotMentions: vi.fn(async (_q?: string) => ({ stocks: [] as unknown[], agents: [] as unknown[] })),
}))

vi.mock('@sida/api', () => ({ chatApi: apiMock, fetchAPI: vi.fn(async () => ({})) }))
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
  getCopilotMentions: copilotMock.getCopilotMentions,
}))
vi.mock('../../src/hooks/useConversations', () => ({ useConversations: () => convMock }))
vi.mock('../../src/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md-fallback">{children}</div>,
}))
vi.mock('../../src/components/copilot/CopilotBriefCard', () => ({ CopilotBriefCard: () => <div /> }))
vi.mock('../../src/components/copilot/LiveRunPanel', () => ({ LiveRunPanel: () => <div /> }))

import AiCopilotPage from '../../src/pages/AiCopilot'
import {
  buildChartContext,
  chartContextParts,
  chartContextPrompt,
  clearChartContextStore,
  publishChartContext,
  type ChartContext,
} from '../../src/lib/copilot/chart-context'
import { resolveTurnSymbol } from '../../src/lib/copilot/symbolChips'

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

function renderPage(entry: { id: number; symbol: string; market?: string } | null) {
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

function detail(id: number, stock: string | null, text: string) {
  return {
    conversation: { id, title: `会话 ${id}`, stock_symbol: stock, created_at: '' },
    messages: [
      { id: 1, role: 'user', content: text, created_at: '' },
      { id: 2, role: 'assistant', content: `收到（${id}）。`, created_at: '' },
    ],
  }
}

class StubEventSource {
  onmessage: ((e: MessageEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  close() {}
  addEventListener() {}
  removeEventListener() {}
}

function clickNewConversation() {
  const button = screen
    .getAllByText('新对话')
    .find((el) => el.tagName === 'BUTTON' && !el.closest('[data-testid="copilot-conv-sidebar"]'))
  // 工具条上的「新对话」只在 convId 存在时渲染（还没建会话时只有侧栏那颗）
  if (button) {
    fireEvent.click(button)
    return
  }
  fireEvent.click(screen.getByTestId('copilot-conv-new'))
}

function publishHistoryItem(id: number, title = '历史会话') {
  convMock.conversations = [{ id, title, created_at: '' }]
  fireEvent.change(screen.getByTestId('copilot-conv-search'), { target: { value: title } })
}

async function openConversation(id: number) {
  await act(async () => {
    fireEvent.click(await screen.findByTestId(`copilot-conv-title-${id}`))
  })
}

beforeEach(() => {
  ;(globalThis as unknown as { EventSource: unknown }).EventSource = StubEventSource
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  localStorage.setItem('token', token(OWNER))
  window.history.pushState({}, '', '/ai-copilot')
  sessionStorage.clear()
  clearChartContextStore()
  convMock.conversations = []
  convMock.remove.mockClear()
  convMock.remove.mockResolvedValue({ ok: true, error: undefined })
  copilotMock.getCopilotMentions.mockReset()
  copilotMock.getCopilotMentions.mockResolvedValue({ stocks: [], agents: [] })
  apiMock.deleteConversation.mockReset()
  apiMock.createConversation.mockReset()
  apiMock.createConversation.mockResolvedValueOnce({ id: 21 }).mockResolvedValue({ id: 22 })
  apiMock.listConversations.mockReset()
  apiMock.listConversations.mockResolvedValue([])
  apiMock.getConversation.mockReset()
  apiMock.getConversation.mockResolvedValue(detail(21, '600111', '先看看 600111'))
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

describe('① 重开历史会话失败 / 在飞：旧证据不许丢', () => {
  it('已在 21（带快照）时重开它却 GET 失败：chip 与「已送入」保留，随后换票仍新开会话', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(21)
    expect((screen.getByTestId('copilot-chart-context').textContent || '')).toContain('已送入本会话')

    apiMock.getConversation.mockRejectedValueOnce(new Error('network'))
    publishHistoryItem(21, '带快照的会话')
    await openConversation(21)
    await waitFor(() => expect(apiMock.getConversation).toHaveBeenCalledWith(21))
    await waitFor(() => expect(screen.getByText(/打开历史对话失败/)).toBeTruthy())

    // 打不开 ≠ 这个会话没带过快照：条和「已送入 · 不可撤回」必须还在
    const chip = screen.getByTestId('copilot-chart-context')
    expect(chip.textContent || '').toContain('已送入本会话')
    expect(chip.textContent || '').toContain('600111')

    await ask('000776 怎么样')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(2))
    expect(apiMock.sendMessageStream.mock.calls[1][0]).not.toBe(21)
    expect(apiMock.sendMessageStream.mock.calls[1][0]).toBe(22)
    expect(createdPayloads()[1].stock_symbol).toBe('000776')
    expect(createdPayloads()[1].initial_context || '').not.toContain('图表上下文')
  })

  it('重开 21 的 GET 还在飞：这一轮发不出去（不会把 000776 送进 21）', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))

    let release: (v: unknown) => void = () => {}
    apiMock.getConversation.mockReturnValueOnce(new Promise((r) => { release = r }))
    publishHistoryItem(21, '带快照的会话')
    await openConversation(21)
    await waitFor(() => expect(apiMock.getConversation).toHaveBeenCalledWith(21))

    typeQuestion('000776 怎么样')
    const sendBtn = screen.getByLabelText('发送') as HTMLButtonElement
    await waitFor(() => expect(sendBtn.disabled).toBe(true))
    fireEvent.click(sendBtn)
    // 在飞窗口里既不许发，也不许把这条送去任何会话
    expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1)
    await act(async () => { release(detail(21, '600111', '先看看 600111')) })
  })

  it('连着点开 21 与 22：先发的 21 晚到，不许覆盖 22 的视图', async () => {
    renderPage(null)
    let release21: (v: unknown) => void = () => {}
    apiMock.getConversation.mockImplementation(async (id: number) => {
      if (id === 21) return new Promise((r) => { release21 = r })
      return detail(22, '000776', '会话二的正文')
    })
    publishHistoryItem(21, '会话一')
    convMock.conversations = [
      { id: 21, title: '会话一', created_at: '' },
      { id: 22, title: '会话二', created_at: '' },
    ]
    await openConversation(21)
    await openConversation(22)
    await waitFor(() => expect(screen.getByText('会话二的正文')).toBeTruthy())

    await act(async () => { release21(detail(21, '600111', '会话一的正文')) })
    await waitFor(() => expect(screen.getByText('会话二的正文')).toBeTruthy())
    expect(screen.queryByText('会话一的正文')).toBeNull()
  })
})

describe('② 刷新后重开历史会话：换票必须新会话（旧会话保留）', () => {
  it('新挂载开 21（600111）再问 000776 → 新会话 22，不再送进 21', async () => {
    // 新会话 id 固定为 22：免得 createConversation 的"第一次返回 21"把新会话伪装成老会话
    apiMock.createConversation.mockReset()
    apiMock.createConversation.mockResolvedValue({ id: 22 })
    renderPage(null)
    publishHistoryItem(21, '带快照的会话')
    await openConversation(21)
    await waitFor(() => expect(apiMock.getConversation).toHaveBeenCalledWith(21))

    await ask('000776 怎么样')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(22)
    expect(createdPayloads()[0].stock_symbol).toBe('000776')
    expect(apiMock.deleteConversation).not.toHaveBeenCalled()
    expect(convMock.remove).not.toHaveBeenCalled()
  })

  it('不可核验的提示要写清后果（绑定标的 + 换标就新开），不是泛化一句', async () => {
    renderPage(null)
    publishHistoryItem(21, '带快照的会话')
    await openConversation(21)
    const notice = await screen.findByTestId('copilot-chart-context-history-notice')
    const text = notice.textContent || ''
    expect(text).toContain('600111')
    expect(text).toContain('新开')
    expect(text).toContain('保留')
  })

  it('同一个标的继续追问：仍留在该会话（不是一律新开）', async () => {
    renderPage(null)
    publishHistoryItem(21, '带快照的会话')
    await openConversation(21)
    await ask('这个位置量能怎么看')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(21)
  })

  it('历史会话没有绑定标的（详情无 stock_symbol）→ 不猜，任何提问都新开会话', async () => {
    apiMock.getConversation.mockResolvedValueOnce(detail(23, null, '没头没尾的一句话'))
    renderPage(null)
    publishHistoryItem(23, '未知会话')
    await openConversation(23)
    await waitFor(() => expect(apiMock.getConversation).toHaveBeenCalledWith(23))
    const notice = await screen.findByTestId('copilot-chart-context-history-notice')
    expect(notice.textContent || '').toContain('未绑定')

    await ask('这票还能拿吗')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).not.toBe(23)
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(21)
  })
})

describe('③ 后台换票识别：只认正文代码是不够的', () => {
  it('后台问「看看宁德时代怎么样」（正文没有代码）→ 不复用带快照的 21', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByTestId('copilot-bg-toggle'))
    await ask('看看宁德时代怎么样')
    await waitFor(() => expect(jobsMock.submitCopilotRun).toHaveBeenCalledTimes(1))
    expect(jobsMock.submitCopilotRun.mock.calls[0][1]).toBeNull()
  })

  it('后台 @ 指定另一只票 → 不复用带快照的 21', async () => {
    copilotMock.getCopilotMentions.mockResolvedValue({
      stocks: [{ type: 'stock', symbol: '300750', name: '宁德时代', market: 'SZ' }],
      agents: [],
    })
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByTestId('copilot-bg-toggle'))
    const ta = screen.getByPlaceholderText(/问点什么|补充说明/) as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: '@宁德' } })
    await waitFor(() => expect(copilotMock.getCopilotMentions).toHaveBeenCalled())
    await act(async () => {
      fireEvent.keyDown(ta, { key: 'ArrowDown' })
      fireEvent.keyDown(ta, { key: 'Enter' })
    })
    await waitFor(() => expect(screen.getByTestId('copilot-mention-chips')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('发送'))
    await waitFor(() => expect(jobsMock.submitCopilotRun).toHaveBeenCalledTimes(1))
    expect(jobsMock.submitCopilotRun.mock.calls[0][1]).toBeNull()
  })
})

describe('④ 后台新会话：不做假「不可核验」，也不抢视图', () => {
  it('后台换票新开会话 22：不挂「历史会话不可核验」，之后重开它也不挂', async () => {
    jobsMock.getCopilotRunStatus.mockResolvedValue({ run_id: 'run-1', status: 'ok', conversation_id: 22 })
    apiMock.getConversation.mockImplementation(async (id: number) =>
      id === 22 ? detail(22, '000776', '000776 怎么样') : detail(21, '600111', '先看看 600111'),
    )
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByTestId('copilot-bg-toggle'))
    await ask('000776 怎么样')
    await waitFor(() => expect(apiMock.getConversation).toHaveBeenCalledWith(22), { timeout: 8000 })
    expect(screen.queryByTestId('copilot-chart-context-history-notice')).toBeNull()

    clickNewConversation()
    publishHistoryItem(22, '后台新会话')
    await openConversation(22)
    await waitFor(() => expect(screen.getByText('000776 怎么样')).toBeTruthy())
    expect(screen.queryByTestId('copilot-chart-context-history-notice')).toBeNull()
  })

  it('后台轮进行中用户已切到 21：完成时不许把视图抢回 22', async () => {
    let finishRun: (v: unknown) => void = () => {}
    jobsMock.getCopilotRunStatus.mockReturnValue(new Promise((r) => { finishRun = r }))
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByTestId('copilot-bg-toggle'))
    await ask('000776 怎么样')
    await waitFor(() => expect(jobsMock.submitCopilotRun).toHaveBeenCalledTimes(1))

    // 用户不等了，自己去点开另一个会话
    apiMock.getConversation.mockClear()
    publishHistoryItem(21, '带快照的会话')
    await openConversation(21)
    await waitFor(() => expect(apiMock.getConversation).toHaveBeenCalledWith(21))

    await act(async () => {
      finishRun({ run_id: 'run-1', status: 'ok', conversation_id: 22 })
    })
    await waitFor(() => expect(jobsMock.getCopilotRunStatus).toHaveBeenCalled(), { timeout: 6000 })
    // 视图仍停在 21，后台结果的会话只记账、不自动跳过去
    expect(apiMock.getConversation).not.toHaveBeenCalledWith(22)
    expect(screen.getByText('先看看 600111')).toBeTruthy()
  })
})

describe('⑤ 建会话在飞时被切走：会话落库了，界面必须承认', () => {
  it('create 返回前点开 21：出现保守提示，且之后换票不送进 21', async () => {
    let resolveCreate: (v: { id: number }) => void = () => {}
    apiMock.createConversation.mockReset()
    apiMock.createConversation.mockReturnValueOnce(new Promise((r) => { resolveCreate = r as never }))
    apiMock.createConversation.mockResolvedValue({ id: 22 })
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.createConversation).toHaveBeenCalledTimes(1))
    expect(String(createdPayloads()[0].initial_context)).toContain('图表上下文')

    publishHistoryItem(21, '带快照的会话')
    await openConversation(21)
    await waitFor(() => expect(apiMock.getConversation).toHaveBeenCalledWith(21))
    await act(async () => { resolveCreate({ id: 21 }) })

    // 会话带着快照落库了，问题却没发出 —— 必须说出来（不许只剩泛化提示）
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    const text = notice.textContent || ''
    expect(text).toContain('21')
    expect(text).toContain('图表快照')
    expect(text).toMatch(/没有发出|未发出/)

    await ask('000776 怎么样')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).not.toBe(21)
  })

  it('create 返回前点「新对话」：同样如实提示，不假装什么都没发生', async () => {
    let resolveCreate: (v: { id: number }) => void = () => {}
    apiMock.createConversation.mockReset()
    apiMock.createConversation.mockReturnValueOnce(new Promise((r) => { resolveCreate = r as never }))
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.createConversation).toHaveBeenCalledTimes(1))

    clickNewConversation()
    await act(async () => { resolveCreate({ id: 21 }) })
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent || '').toContain('21')
    expect(notice.textContent || '').toMatch(/没有发出|未发出/)
    expect(apiMock.sendMessageStream).not.toHaveBeenCalled()
  })
})

describe('⑥ 周期白名单不走原型链', () => {
  it.each(['constructor', 'toString', 'hasOwnProperty', 'valueOf', '__proto__'])(
    '?period=%s 不算合法周期，不进 prompt／分段',
    (period) => {
      const ctx = buildChartContext({
        owner: OWNER,
        symbol: '600111',
        instrument: 'stock',
        market: 'CN',
        period,
        now: Date.now(),
      }) as ChartContext
      expect(ctx.period).toBeUndefined()
      const prompt = chartContextPrompt(ctx)
      expect(prompt).not.toContain('function Object')
      expect(prompt).not.toContain('function')
      const parts = chartContextParts(ctx)
      expect(parts.every((p) => typeof p === 'string')).toBe(true)
      expect(parts.join(' ')).not.toContain('function')
    },
  )

  it('合法周期照旧显示人话', () => {
    const ctx = buildChartContext({
      owner: OWNER,
      symbol: '600111',
      instrument: 'stock',
      market: 'CN',
      period: 'd1',
      now: Date.now(),
    }) as ChartContext
    expect(chartContextParts(ctx).join(' ')).toContain('日线')
  })
})

describe('⑦ 本轮标的解析：@ 指定优先于正文与锚点', () => {
  it('mention 的代码优先', () => {
    expect(resolveTurnSymbol({ mentioned: '300750', text: '600111 怎么样', anchor: '600000' })).toEqual({
      symbol: '300750',
      explicit: true,
    })
  })

  it('正文代码次之，最后才是页面锚点（且标记为不可证）', () => {
    expect(resolveTurnSymbol({ mentioned: '', text: '看看 300750', anchor: '600000' })).toEqual({
      symbol: '300750',
      explicit: true,
    })
    expect(resolveTurnSymbol({ mentioned: '', text: '再看看', anchor: '600000' })).toEqual({
      symbol: '600000',
      explicit: false,
    })
    expect(resolveTurnSymbol({ mentioned: '', text: '再看看', anchor: '' })).toEqual({ symbol: '', explicit: false })
  })
})

describe('⑧ 切账号 / 删除当前会话：显示跟着清', () => {
  it('同一标签页换账号：chip 消失并说明，不宣称删掉了服务端记录', async () => {
    publishChartContext(workbenchContext({ owner: OWNER }))
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')

    localStorage.setItem('token', token(OTHER_OWNER))
    await act(async () => { window.dispatchEvent(new Event('focus')) })

    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent || '').toContain('账号')
    expect(notice.textContent || '').not.toMatch(/已删除|已撤回/)
  })

  it('换号后在同一只票上追问：也不许把新账号的问题送进上一个账号的会话', async () => {
    publishChartContext(workbenchContext({ owner: OWNER }))
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(21)

    localStorage.setItem('token', token(OTHER_OWNER))
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())

    await ask('600111 现在怎么看')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(2))
    // chip 被撤掉之后不能再靠 chartCtx 判断串号：会话归属本身也要核
    expect(apiMock.sendMessageStream.mock.calls[1][0]).not.toBe(21)
    expect(createdPayloads()[1].initial_context || '').not.toContain('图表上下文')
  })

  it('删除当前会话：视图清空，下一次提问不会送回 21', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))

    publishHistoryItem(21, '带快照的会话')
    fireEvent.click(screen.getByTestId('copilot-conv-menu-btn-21'))
    fireEvent.click(await screen.findByTestId('copilot-conv-menu-delete-21'))
    fireEvent.click(await screen.findByTestId('copilot-conv-confirm-21'))
    await waitFor(() => expect(convMock.remove).toHaveBeenCalledWith(21))

    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())
    expect(screen.queryByTestId('copilot-chart-context-history-notice')).toBeNull()
    expect(screen.queryByText('收到。')).toBeNull()

    await ask('000776 怎么样')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(2))
    expect(apiMock.sendMessageStream.mock.calls[1][0]).not.toBe(21)
  })
})
