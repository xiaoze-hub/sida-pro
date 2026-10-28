// @vitest-environment jsdom
/**
 * 参谋页 · 流中途断掉（真实验收 P0, 2026-10-28）。
 *
 * 真实现场：`sendMessageStream` 已经推了几段 delta（屏幕上有了几行正文），
 * 随后网络层 reject —— 这时候界面上那半截正文**原样亮着**，没有任何"这不是成品"的标记，
 * 用户会当成结论读、甚至据此下单。但终稿根本没拿到。
 *
 * 口径：fetch 中途 reject（以及坏帧失败）→ 半截正文必须带 `copilot-stream-incomplete`
 * 可见标记，同时把失败原文挂出来；**只有当前这一轮的主人**能打这个标记 ——
 * 切走后的旧流不许把标记/正文落到新会话上。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
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

interface Pending {
  handlers: { onStage?: (s: string) => void; onDelta?: (d: string) => void; onDone?: (m: unknown) => void; onError?: (e: string) => void; onMalformed?: () => void }
  resolve: (result?: unknown) => void
  reject: (err: unknown) => void
}

const pending: Pending[] = []

function hangStreams() {
  pending.length = 0
  apiMock.sendMessageStream.mockImplementation(
    (_id: number, _q: string, handlers: Pending['handlers']) =>
      new Promise((resolve, reject) => {
        pending.push({ handlers, resolve, reject })
      }),
  )
}

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  pending.length = 0
  apiMock.sendMessageStream.mockReset()
  apiMock.createConversation.mockReset()
  apiMock.createConversation.mockResolvedValue({ id: 21, title: '' })
  apiMock.getConversation.mockReset()
  apiMock.getConversation.mockResolvedValue({
    conversation: { id: 9, title: '旧对话', stock_symbol: null, created_at: '' },
    messages: [
      { id: 1, role: 'user', content: '旧问题', created_at: '' },
      { id: 2, role: 'assistant', content: '旧回答：主力净流出', created_at: '' },
    ],
  })
  window.history.pushState({}, '', '/ai-copilot')
})

afterEach(cleanup)

async function ask(question: string) {
  fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: question } })
  await act(async () => {
    fireEvent.click(screen.getByLabelText('发送'))
  })
  await act(async () => {
    await Promise.resolve()
  })
}

describe('流中途 reject · 半截正文必须标成不完整', () => {
  it('已有半截正文时断流：正文保留但带不完整标记，失败原因可见', async () => {
    hangStreams()
    render(<AiCopilotPage />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())

    await ask('600111 怎么看')
    await waitFor(() => expect(pending.length).toBe(1))

    await act(async () => {
      pending[0].handlers.onStage?.('正在取数 3/10：get_main_intent')
      pending[0].handlers.onDelta?.('先给结论：主力净流出约 1.2 亿。')
    })
    expect(screen.getByText(/主力净流出约 1\.2 亿/)).toBeTruthy()
    // 流还在跑：此刻不是"不完整"，就是正常生成中
    expect(screen.queryByTestId('copilot-stream-incomplete')).toBeNull()

    // fetch 中途 reject（代理超时 / 连接被重置）
    await act(async () => {
      pending[0].reject(new Error('网络中断：socket hang up'))
    })
    await act(async () => {
      await Promise.resolve()
    })

    const flag = await waitFor(() => screen.getByTestId('copilot-stream-incomplete'))
    expect(flag.textContent).toMatch(/不完整/)
    // 半截正文不许被抹掉（用户看得见写了什么），但也不能被当成定稿
    expect(screen.getByText(/主力净流出约 1\.2 亿/)).toBeTruthy()
    expect(screen.getByText(/socket hang up/)).toBeTruthy()
  })

  it('首字还没到就断流：不铺空气泡，只报错', async () => {
    hangStreams()
    render(<AiCopilotPage />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())

    await ask('600111 怎么看')
    await waitFor(() => expect(pending.length).toBe(1))

    await act(async () => {
      pending[0].reject(new Error('网络中断：socket hang up'))
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.queryByTestId('copilot-stream-incomplete')).toBeNull()
    expect(await screen.findByText(/socket hang up/)).toBeTruthy()
  })

  it('切走的旧流断在半截：标记与正文都不许落到新会话上', async () => {
    hangStreams()
    render(<AiCopilotPage />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())

    await ask('600111 怎么看')
    await waitFor(() => expect(pending.length).toBe(1))

    await act(async () => {
      pending[0].handlers.onDelta?.('旧会话半截正文')
    })
    expect(screen.getByText(/旧会话半截正文/)).toBeTruthy()

    // 切到历史会话（旧流从此刻起不再是主人）
    fireEvent.click(screen.getByTestId('copilot-conv-title-9'))
    await waitFor(() => expect(screen.getByText(/旧回答：主力净流出/)).toBeTruthy())
    expect(screen.queryByText(/旧会话半截正文/)).toBeNull()

    await act(async () => {
      pending[0].reject(new Error('旧流中断：socket hang up'))
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.queryByTestId('copilot-stream-incomplete')).toBeNull()
    expect(screen.queryByText(/旧会话半截正文/)).toBeNull()
    expect(screen.queryByText(/socket hang up/)).toBeNull()
    // 历史回答没有被标记成不完整
    expect(screen.getByText(/旧回答：主力净流出/)).toBeTruthy()
  })
})
