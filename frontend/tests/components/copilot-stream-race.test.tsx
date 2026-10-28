// @vitest-environment jsdom
/**
 * 参谋页 · 流式归属竞态（P0, 2026-10-28）。
 *
 * 上一轮（3cda4059）给流加了归属令牌，只挡住了「旧流的正文写进新会话」。
 * 收尾路径没挡：旧流的 `catch`/`finally` 仍然无条件写共享态 ——
 *   - `catch` → `setError(旧流的失败文案)`：用户在会话 B 里，横幅却报会话 A 的错；
 *   - `finally` → `setStreaming(false)`：会话 B 里正在跑的流被抹掉 busy 态，发送键解锁、
 *     loading 消失，用户以为答完了；
 *   - `finally` → `setStage('')`：会话 B 的取数阶段提示被清空；
 *   - `finally` 的 else 分支 → `setReadiness(emptyReadiness())`：会话 B 已取到的证据齐套度被清空。
 *
 * 复现路径全是真实操作序列（同一页面内）：
 *   ① 会话 A 发问 → 旧流挂在网络上（真 send 路径，mock 只替换网络层）；
 *   ② 切到历史会话（loading 属于上一轮视图，切走即收）；
 *   ③ 在新会话再发一轮 → 新流；
 *   ④ 旧流此时失败/收尾 → 不许碰新流的 loading / stage / 齐套度 / 错误。
 *
 * 真数据纪律：渲染真页面、走真 send 逻辑，只 mock `@sida/api` 网络层与重组件。
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

/** 一条挂在网络上、由用例决定何时收尾的流。 */
interface Pending {
  handlers: any
  resolve: (result?: unknown) => void
  reject: (err: unknown) => void
}

const pending: Pending[] = []

/** 让每一轮流都停在"服务端还在跑"的状态（不用定时器，用例自己放行）。 */
function hangStreams() {
  pending.length = 0
  apiMock.sendMessageStream.mockImplementation(
    (_id: number, _q: string, handlers: any) =>
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
      { id: 2, role: 'assistant', content: '旧回答', created_at: '' },
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

describe('旧流的收尾不得污染新会话 / 新流', () => {
  it('旧流在新流跑到一半失败：不改新流的 loading、stage、齐套度，也不把旧错误挂到新会话', async () => {
    hangStreams()
    let busy = false
    render(<AiCopilotPage onBusyChange={(b) => { busy = b }} />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())

    // ① 会话 A 发问 → 旧流挂起
    await ask('600111 怎么看')
    await waitFor(() => expect(pending.length).toBe(1))
    expect(busy).toBe(true)

    // ② 中途切到历史会话：上一轮的 loading 属于上一轮视图，切走即收
    fireEvent.click(screen.getByTestId('copilot-conv-title-9'))
    await waitFor(() => expect(screen.getByText('旧回答')).toBeTruthy())
    expect(busy).toBe(false)

    // ③ 在新会话再发一轮（真 concurrent：旧流还没收尾）
    await ask('002636 还能追吗')
    await waitFor(() => expect(pending.length).toBe(2))
    const oldStream = pending[0]
    const newStream = pending[1]
    await act(async () => {
      newStream.handlers.onStage('正在取数 3/10：get_main_intent')
      newStream.handlers.onDelta('新流草稿')
      newStream.handlers.onStage('正在核对资金口径…')
    })

    expect(busy).toBe(true)
    expect(screen.getByText('正在核对资金口径…')).toBeTruthy()
    expect(screen.getByTestId('evidence-readiness')).toBeTruthy()

    // ④ 旧流这一轮失败收尾（网络中断）—— 它已经不是当前视图的主人
    await act(async () => {
      oldStream.reject(new Error('旧流中断：HTTP 502'))
    })
    await act(async () => {
      await Promise.resolve()
    })

    // 旧错误不许出现在用户正看着的这一轮
    expect(screen.queryByText(/HTTP 502/)).toBeNull()
    // 新流的 loading / stage / 齐套度都还在（旧流的 finally 无权清）
    expect(busy).toBe(true)
    expect(screen.getByText('正在核对资金口径…')).toBeTruthy()
    expect(screen.getByTestId('evidence-readiness')).toBeTruthy()

    // ⑤ 新流自己收尾：终稿上屏，loading 这时才收回
    await act(async () => {
      newStream.handlers.onDone({ id: 30, role: 'assistant', content: '新流终稿：主力净流出，别追。', created_at: '' })
      newStream.resolve({ content: '新流终稿：主力净流出，别追。', authoritative: true, corruptedFrames: 0, errors: [] })
    })

    await waitFor(() => expect(screen.getByText(/新流终稿/)).toBeTruthy())
    expect(busy).toBe(false)
    expect(screen.queryByText(/HTTP 502/)).toBeNull()
    expect(screen.queryByTestId('copilot-stream-incomplete')).toBeNull()
  })

  it('切走后旧流失败：错误只属于它自己那一轮，新会话里不留别人的错误条', async () => {
    hangStreams()
    let busy = false
    render(<AiCopilotPage onBusyChange={(b) => { busy = b }} />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())

    await ask('600111 怎么看')
    await waitFor(() => expect(pending.length).toBe(1))

    fireEvent.click(screen.getByTestId('copilot-conv-title-9'))
    await waitFor(() => expect(screen.getByText('旧回答')).toBeTruthy())

    await act(async () => {
      pending[0].reject(new Error('旧流中断：HTTP 502'))
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.queryByText(/HTTP 502/)).toBeNull()
    expect(busy).toBe(false)
    expect(screen.getByText('旧回答')).toBeTruthy()
  })

  it('旧流迟到的终稿/坏帧提示也不落到新会话上（气泡与提示都不串）', async () => {
    hangStreams()
    let busy = false
    render(<AiCopilotPage onBusyChange={(b) => { busy = b }} />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())

    await ask('600111 怎么看')
    await waitFor(() => expect(pending.length).toBe(1))

    fireEvent.click(screen.getByTestId('copilot-conv-title-9'))
    await waitFor(() => expect(screen.getByText('旧回答')).toBeTruthy())

    await act(async () => {
      pending[0].handlers.onDelta('旧流草稿')
      pending[0].handlers.onDone({ id: 5, role: 'assistant', content: '旧流终稿', created_at: '' })
      pending[0].resolve({ content: '旧流终稿', authoritative: true, corruptedFrames: 2, errors: [] })
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.queryByText(/旧流终稿/)).toBeNull()
    expect(screen.queryByText(/旧流草稿/)).toBeNull()
    // 坏帧提示也不许挂到别人屏幕上
    expect(screen.queryByTestId('copilot-stream-notice')).toBeNull()
    expect(busy).toBe(false)
  })
})
