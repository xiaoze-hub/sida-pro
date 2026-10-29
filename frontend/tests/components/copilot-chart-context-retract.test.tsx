// @vitest-environment jsdom
/**
 * 图表上下文「撤回」语义（P2 修正, 2026-10-29）。
 *
 * 事实链（后端 `src/web/api/chat.py` 实测，不是推测）：
 *  - `POST /chat/conversations` 把 `initial_context` 落库到 `chat_conversations.initial_context`；
 *  - `_build_ai_messages()` **每一轮**都无条件把 `conv.initial_context` 拼进 system prompt，
 *    且 `_bind_conv_symbol()` 换票时只改 `stock_symbol`，不动 `initial_context`；
 *  - 现有路由只有 POST/GET/DELETE，没有任何接口能改/清 `initial_context`。
 * 所以「首问之后把本地 chip 点掉」「换标的继续问」都不会让服务端少收到一次旧快照 ——
 * 页面说已清除、模型却还在读旧区间，是假话。
 *
 * 本文件锁死修正后的语义（用真实参谋页 + 真实 API 契约，只 mock 网络层）：
 *  ① 首问之后清除：下一次追问**不得**再发往那个已带快照的会话；必须新开一个不带它的会话，
 *     旧会话保留（不 DELETE），并且界面明说上一会话仍带着快照、无法撤回；
 *  ② 首问之后改标的：同上（旧会话的 initial_context 不会因换票消失）；
 *  ③ 还没发问就清除：什么都没送给服务端，不新建会话，也不谎称"已从服务端撤掉"；
 *  ④ 正在流式回答时清除：不打断本轮（不 abort、不换会话），按钮不可用并给出原因；
 *  ⑤ 上下文只活在内存里（不写 sessionStorage），换账号读不到别人的快照；
 *  ⑥ chip 常显「进入时快照 · 非实时」，送进会话后明示"已送入/不可撤回"。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  sendMessageStream: vi.fn(),
  createConversation: vi.fn(),
  listConversations: vi.fn(async () => [{ id: 9, title: '旧对话', stock_symbol: null, created_at: '' }]),
  getConversation: vi.fn(),
  deleteConversation: vi.fn(),
}))

const convMock = vi.hoisted(() => ({
  conversations: [{ id: 9, title: '旧对话', stock_symbol: null, created_at: '' }],
  list: vi.fn(async () => {}),
  rename: vi.fn(async () => ({ ok: true })),
  pin: vi.fn(async () => ({ ok: true })),
  remove: vi.fn(async () => ({ ok: true })),
}))

vi.mock('@sida/api', () => ({ chatApi: apiMock }))
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

import AiCopilotPage from '../../src/pages/AiCopilot'
import {
  CHART_CONTEXT_TTL_MS,
  buildChartContext,
  chartContextPrompt,
  chartContextStoreSize,
  clearChartContextStore,
  publishChartContext,
  readChartContext,
  type ChartContext,
} from '../../src/lib/copilot/chart-context'

const OWNER = 'test-user'

function workbenchContext(overrides: Record<string, unknown> = {}): ChartContext {
  return buildChartContext({
    owner: OWNER,
    symbol: '600111',
    instrument: 'stock',
    market: 'CN',
    period: 'd1',
    range: { from: '2026-06-01', to: '2026-10-29', bars: 58, changePct: 12.34, amplitudePct: 23.45 },
    levels: [
      { price: 12.34, kind: 'support', label: '底仓成本' },
      { price: 15.2, kind: 'pressure', label: '压力' },
    ],
    // 读侧有 5 分钟 TTL：fixture 必须是"刚刚抓的"，否则会被判过期
    now: Date.now(),
    ...overrides,
  }) as ChartContext
}

function renderPage(entry: { id: number; symbol: string; market?: string; pageContext?: string } | null) {
  return render(<AiCopilotPage entry={entry} />)
}

async function ask(question: string) {
  await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
  fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: question } })
  fireEvent.click(screen.getByLabelText('发送'))
}

function createdPayloads() {
  return apiMock.createConversation.mock.calls.map((c) => c[0] as { stock_symbol?: string; initial_context?: string })
}

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: OWNER }))}.signature`)
  window.history.pushState({}, '', '/ai-copilot')
  sessionStorage.clear()
  clearChartContextStore()
  apiMock.deleteConversation.mockReset()
  apiMock.createConversation.mockReset()
  apiMock.createConversation.mockResolvedValueOnce({ id: 21 }).mockResolvedValue({ id: 22 })
  apiMock.sendMessageStream.mockReset()
  apiMock.sendMessageStream.mockImplementation(async (_id: number, _q: string, handlers: any) => {
    handlers.onDone({ id: 7, role: 'assistant', content: '收到。', created_at: '' })
    return { content: '收到。', authoritative: true, corruptedFrames: 0, errors: [] }
  })
})

afterEach(() => {
  cleanup()
  localStorage.removeItem('token')
  sessionStorage.clear()
  clearChartContextStore()
})

describe('首问之后清除：后端不能再读旧快照', () => {
  it('清除后追问 → 新会话（不带图表上下文），旧会话不再收到消息且不被删除', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('这个区间的量能怎么看')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(21)
    expect(createdPayloads()[0].initial_context).toContain('图表上下文')

    // 首问已把快照写进 21 —— 此时界面必须承认它撤不回来
    const chip = screen.getByTestId('copilot-chart-context')
    expect(chip.textContent).toContain('已送入本会话')
    expect(chip.textContent).toContain('进入时快照')
    expect(chip.textContent).toContain('非实时')
    // 送出去的每一项都要看得见：抓取时刻 / 振幅 / 价位方向与用户标签
    expect(chip.textContent).toContain('抓取于')
    expect(chip.textContent).toContain('振幅 23.45%')
    expect(chip.textContent).toContain('支撑·底仓成本')
    expect(chip.textContent).toContain('压力·压力')
    const clearBtn = screen.getByTestId('copilot-chart-context-clear')
    expect(clearBtn.textContent).toContain('新开会话')

    fireEvent.click(clearBtn)
    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent).toContain('上一会话')
    expect(notice.textContent).toContain('无法撤回')

    await ask('那支撑位呢')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(2))
    // 关键：第二问必须落在新会话上，不能复用 21（后端每轮都会把 21 的 initial_context 拼进系统提示）
    expect(apiMock.sendMessageStream.mock.calls[1][0]).toBe(22)
    expect(apiMock.createConversation).toHaveBeenCalledTimes(2)
    expect(createdPayloads()[1].initial_context || '').not.toContain('图表上下文')
    // 不误删对话
    expect(apiMock.deleteConversation).not.toHaveBeenCalled()
  })

  it('首问之后改问另一只票 → 新会话承接新一轮，旧会话的快照不再被复用', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(apiMock.sendMessageStream.mock.calls[0][0]).toBe(21)

    await ask('000776 怎么样')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(2))
    expect(apiMock.sendMessageStream.mock.calls[1][0]).toBe(22)
    const second = createdPayloads()[1]
    expect(second.stock_symbol).toBe('000776')
    expect(second.initial_context || '').not.toContain('图表上下文')
    expect(second.initial_context || '').not.toContain('2026-06-01')
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent).toContain('新开')
    expect(apiMock.deleteConversation).not.toHaveBeenCalled()
  })

  it('首问之后点「换标的」不会把 chip 藏起来假装没了；旧会话仍带快照需明说', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('先看看 600111')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(1))

    const anchor = screen.getByTestId('copilot-context-anchor')
    fireEvent.click(anchor.querySelector('button') as HTMLButtonElement)
    // 会话 21 里那份快照没变，chip 必须还在（不能"眼不见为净"）
    expect(screen.getByTestId('copilot-chart-context')).toBeTruthy()
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent).toContain('本会话')
  })
})

describe('还没发问就清除：服务端从未收到', () => {
  it('不新建会话、不谎称"已从服务端撤掉"', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    expect(screen.getByTestId('copilot-chart-context').textContent).toContain('尚未送出')

    fireEvent.click(screen.getByTestId('copilot-chart-context-clear'))
    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent).toContain('尚未')
    expect(notice.textContent).not.toContain('上一会话')

    await ask('这个区间怎么看')
    await waitFor(() => expect(apiMock.createConversation).toHaveBeenCalledTimes(1))
    expect(createdPayloads()[0].initial_context || '').not.toContain('图表上下文')
    expect(chartContextStoreSize()).toBe(0)
  })
})

describe('不打断正在进行的回答', () => {
  it('流式中点清除 → 不 abort、不换会话，按钮不可用且说明原因', async () => {
    let midHandlers: any = null
    let seenSignal: AbortSignal | undefined
    apiMock.sendMessageStream.mockImplementation((_id: number, _q: string, handlers: any, signal: AbortSignal) => {
      midHandlers = handlers
      seenSignal = signal
      return new Promise(() => {})
    })
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('这个区间的量能怎么看')
    await waitFor(() => expect(midHandlers).not.toBeNull())

    const clearBtn = screen.getByTestId('copilot-chart-context-clear') as HTMLButtonElement
    await waitFor(() => expect(clearBtn.disabled).toBe(true))
    fireEvent.click(clearBtn)
    expect(seenSignal?.aborted).toBe(false)
    expect(screen.getByTestId('copilot-chart-context')).toBeTruthy()
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
  })
})

describe('隔离与凭据边界', () => {
  it('整条链路不写 sessionStorage（本地存储不能当服务端凭证）', async () => {
    publishChartContext(workbenchContext())
    sessionStorage.clear()
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    // 上下文来自内存仓库，不是 sessionStorage：清掉存储仍然显示
    expect((await screen.findByTestId('copilot-chart-context')).textContent).toContain('58 根')
    await ask('这个区间怎么看')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalled())
    expect(sessionStorage.length).toBe(0)
  })

  it('换账号后读不到别人发布的快照，也不会把别人的快照送进请求', async () => {
    publishChartContext(workbenchContext({ owner: 'someone-else' }))
    expect(readChartContext('600111', 'CN', OWNER)).toBeNull()
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    expect(screen.queryByTestId('copilot-chart-context')).toBeNull()
    await ask('这个区间怎么看')
    await waitFor(() => expect(apiMock.createConversation).toHaveBeenCalled())
    expect(createdPayloads()[0].initial_context || '').not.toContain('图表上下文')
  })

  it('缺证据时不编价位/区间：prompt 只写拿得到的那部分', () => {
    const bare = workbenchContext({ range: null, levels: null, period: null })
    const text = chartContextPrompt(bare)
    expect(text).toContain('进入时快照')
    expect(text).not.toContain('可见区间')
    expect(text).not.toContain('图上价位')
    expect(text).not.toContain('周期：')
  })

  it('切到历史会话后不再把当前图的快照带进去', async () => {
    apiMock.getConversation.mockResolvedValue({
      conversation: { id: 9, title: '旧对话', stock_symbol: null, created_at: '' },
      messages: [{ id: 1, role: 'user', content: '旧问题', created_at: '' }],
    })
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await act(async () => {
      fireEvent.click(await screen.findByText('旧对话'))
    })
    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())
    // 历史会话的快照服务端不回传 → 界面必须持续承认"不可核验"，不能假装没有
    const notice = await screen.findByTestId('copilot-chart-context-history-notice')
    expect(notice.textContent).toContain('历史会话')
    expect(notice.textContent).toContain('无法核验')
    await ask('接着问')
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalled())
    /**
     * 2026-10-29 三审收紧：这个历史会话**没有绑定标的**（`stock_symbol: null`），
     * 而"接着问"本身判不出问的是哪只票 —— 按"无法确认就不复用"处理，这一轮进**不带快照的新会话**。
     * 视图已经是新会话了，所以那条"不可核验"提示跟着旧会话一起退场（不是被藏起来），
     * 取而代之的是"为什么换会话"的真实说明。
     */
    expect(apiMock.createConversation).toHaveBeenCalledTimes(1)
    const payload = apiMock.createConversation.mock.calls[0][0] as { initial_context?: string }
    expect(payload.initial_context || '').not.toContain('图表上下文')
    expect(apiMock.sendMessageStream.mock.calls[0][0]).not.toBe(9)
    expect(apiMock.deleteConversation).not.toHaveBeenCalled()
    const stale = await screen.findByTestId('copilot-chart-context-notice')
    expect(stale.textContent || '').toContain('新开')
  })
})

describe('时效：过期不发送、过期要说明', () => {
  it('进入时快照已超过 TTL → 不显示读数，明确告知已过期、未带入', async () => {
    publishChartContext(workbenchContext({ now: Date.now() - CHART_CONTEXT_TTL_MS - 1000 }))
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    expect(screen.queryByTestId('copilot-chart-context')).toBeNull()
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent).toContain('超过 5 分钟')
    expect(notice.textContent).toContain('未带入')

    await ask('这个区间怎么看')
    await waitFor(() => expect(apiMock.createConversation).toHaveBeenCalled())
    expect(createdPayloads()[0].initial_context || '').not.toContain('图表上下文')
  })

  it('在参谋页停留超过 TTL 再发问 → 旧区间不送出去，并提示过期', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')

    const later = Date.now() + CHART_CONTEXT_TTL_MS + 1000
    const spy = vi.spyOn(Date, 'now').mockReturnValue(later)
    try {
      await ask('这个区间怎么看')
      await waitFor(() => expect(apiMock.createConversation).toHaveBeenCalled())
    } finally {
      spy.mockRestore()
    }
    const payload = createdPayloads()[0]
    expect(payload.initial_context || '').not.toContain('图表上下文')
    expect(payload.initial_context || '').not.toContain('2026-06-01')
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent).toContain('超过 5 分钟')
  })

  it('清除后工作台重绘再次发布同一张图也不会复活', async () => {
    const ctx = workbenchContext()
    publishChartContext(ctx)
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    fireEvent.click(screen.getByTestId('copilot-chart-context-clear'))
    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())

    // 工作台缩放/重绘 → 同一张图再次发布：必须被"已清除"名单拦下
    expect(publishChartContext(ctx)).toBeNull()
    expect(readChartContext('600111', 'CN', OWNER)).toBeNull()
    expect(chartContextStoreSize()).toBe(0)

    // 再次进入参谋：不静默 —— 说明"你清除过，本次未带入"
    cleanup()
    renderPage({ id: 2, symbol: '600111', market: 'CN' })
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    expect(screen.queryByTestId('copilot-chart-context')).toBeNull()
    const notice = await screen.findByTestId('copilot-chart-context-notice')
    expect(notice.textContent).toContain('清除')
    expect(notice.textContent).toContain('未带入')
  })
})
