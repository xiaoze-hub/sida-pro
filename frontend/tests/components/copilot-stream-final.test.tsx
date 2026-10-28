// @vitest-environment jsdom
/**
 * 参谋页 · SSE 终稿与坏帧接线回归（P0, 2026-10-28）。
 *
 * 后端的 `done.content` 是落库全文，可能和 delta 拼接不一样：对账附言只在终稿里、
 * 零工具拒答一个 delta 都不发、断流时根本不发 done。旧接线只认 delta（onDone 没接），
 * 于是附言永远看不见、拒答显示成空气、断流还把半截正文当成品亮着。
 *
 * 本文件证明**真的接上了**（渲染真页面，只 mock 网络层）：
 *  ① 终稿替换流式草稿；
 *  ② 只有终稿（拒答）也能出气泡；
 *  ③ 没有终稿 / 坏帧 → 气泡上带可见的"未拿到终稿"标记，不是静默成功；
 *  ④ 坏帧但终稿到达 → 出可见提示，仍按终稿显示（不静默、不误报失败）；
 *  ⑤ 流式途中切会话 → 旧流的 delta/终稿不会写进新会话（并发不串）。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn(),
  createConversation: vi.fn().mockResolvedValue({ id: 21 }),
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

const DRAFT = '草稿：主力资金净流出。'
const FINAL = '主力净流出 552 万。\n\n（数字对账：2 个数字未在工具原文中找到，已标注）'
const REFUSAL = '这次没取到可核验的数据，不做结论。'

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  apiMock.sendMessageStream.mockReset()
  apiMock.getConversation.mockReset()
  apiMock.getConversation.mockResolvedValue({
    conversation: { id: 9, title: '旧对话', stock_symbol: null, created_at: '' },
    messages: [
      { id: 1, role: 'user', content: '旧问题', created_at: '' },
      { id: 2, role: 'assistant', content: '旧回答', created_at: '' },
    ],
  })
  window.history.pushState({}, '', '/ai-copilot')
})

afterEach(cleanup)

async function ask(question: string) {
  render(<AiCopilotPage />)
  await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
  fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: question } })
  fireEvent.click(screen.getByLabelText('发送'))
}

describe('终稿替换流式草稿', () => {
  it('done.content 与 delta 不同 → 屏上是终稿（附言不丢），草稿不残留', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _q: string, handlers: any) => {
      handlers.onDelta(DRAFT)
      handlers.onDone({ id: 7, role: 'assistant', content: FINAL, created_at: '' })
      return { content: FINAL, authoritative: true, corruptedFrames: 0, errors: [] }
    })

    await ask('600111 怎么样')

    await waitFor(() => expect(screen.getByTestId('md-fallback').textContent).toContain('552 万'))
    expect(screen.getByTestId('md-fallback').textContent).toContain('数字对账')
    expect(screen.queryByText(/草稿：主力资金净流出/)).toBeNull()
    expect(screen.queryByTestId('copilot-stream-incomplete')).toBeNull()
  })
})

describe('只有终稿的拒答', () => {
  it('一个 delta 都没发也不许空气泡：拒答正文可见', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _q: string, handlers: any) => {
      handlers.onDone({ id: 8, role: 'assistant', content: REFUSAL, created_at: '' })
      return { content: REFUSAL, authoritative: true, corruptedFrames: 0, errors: [] }
    })

    await ask('600111 明天涨停吗')

    await waitFor(() => expect(screen.getByTestId('md-fallback').textContent).toContain('不做结论'))
    expect(screen.queryByTestId('copilot-stream-incomplete')).toBeNull()
  })
})

describe('没有终稿 / 坏帧', () => {
  it('断流（无 done）：正文保留但气泡带可见的"未拿到终稿"，且出错误条', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _q: string, handlers: any) => {
      handlers.onDelta('只有一半')
      handlers.onMalformed?.({ reason: 'invalid-json', raw: 'event: delta\ndata: {"content": "半' })
      return {
        content: '只有一半',
        authoritative: false,
        corruptedFrames: 1,
        errors: [],
        failure: '流式数据损坏（1 处），且未收到服务端终稿，回答可能不完整',
      }
    })

    await ask('600111 怎么样')

    await waitFor(() => expect(screen.getByTestId('copilot-stream-incomplete')).toBeTruthy())
    expect(screen.getByTestId('md-fallback').textContent).toContain('只有一半')
    expect(screen.getByText(/未收到服务端终稿/)).toBeTruthy()
  })

  it('坏帧但终稿到达：出可见提示、按终稿显示，不误报失败', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _q: string, handlers: any) => {
      handlers.onMalformed?.({ reason: 'invalid-json', raw: 'event: delta\ndata: {oops' })
      handlers.onDelta(DRAFT)
      handlers.onDone({ id: 12, role: 'assistant', content: FINAL, created_at: '' })
      return { content: FINAL, authoritative: true, corruptedFrames: 1, errors: [] }
    })

    await ask('600111 怎么样')

    await waitFor(() => expect(screen.getByTestId('copilot-stream-notice')).toBeTruthy())
    expect(screen.getByTestId('copilot-stream-notice').textContent).toContain('1 处')
    expect(screen.getByTestId('md-fallback').textContent).toContain('552 万')
    expect(screen.queryByTestId('copilot-stream-incomplete')).toBeNull()
  })
})

describe('并发不串消息', () => {
  it('流式途中切到别的会话：旧流的 delta/终稿不写进新会话', async () => {
    let release: (() => void) | null = null
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _q: string, handlers: any) => {
      await new Promise<void>((resolve) => {
        release = resolve
      })
      handlers.onDelta('串线草稿')
      handlers.onDone({ id: 99, role: 'assistant', content: '串线终稿', created_at: '' })
      return { content: '串线终稿', authoritative: true, corruptedFrames: 0, errors: [] }
    })

    await ask('600111 怎么样')
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())

    // 切到历史会话（旧对话 = 9）
    fireEvent.click(screen.getByTestId('copilot-conv-title-9'))
    await waitFor(() => expect(screen.getByText('旧回答')).toBeTruthy())

    await act(async () => {
      release?.()
    })

    expect(screen.queryByText(/串线终稿/)).toBeNull()
    expect(screen.queryByText(/串线草稿/)).toBeNull()
    expect(screen.getByText('旧回答')).toBeTruthy()
    expect(screen.queryByTestId('copilot-stream-incomplete')).toBeNull()
  })
})
