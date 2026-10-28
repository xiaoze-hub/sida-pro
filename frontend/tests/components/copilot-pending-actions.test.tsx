// @vitest-environment jsdom
/**
 * 参谋页 · 待确认动作（真实验收 P0, 2026-10-28）。
 *
 * 验收现场看到的三件事，都是同一条链断在 `act()`：
 *   ① 按钮没有提交中态 —— 同一个事件循环里连点两次「确认」，两次都发 POST，
 *      后端连记两次（自选加两条/标注画两条）；
 *   ② `act()` 只有 `finally`、没有 `catch` —— 确认失败时 rejection 冒到 `void act(...)`，
 *      变成 unhandled rejection：控制台报错、界面上一个字都没有，用户只能反复点；
 *   ③ 失败时也不该把待确认行清掉 —— 失败 = 没落库，行必须留着让人重试。
 *
 * 口径：失败**就地**报错（挂在这条动作旁边），pending 原样保留；成功才刷新。
 * 真数据纪律：渲染真页面、走真 `act`，只 mock 网络层与重组件。
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
  conversations: [],
  list: vi.fn(async () => {}),
  rename: vi.fn(async () => ({ ok: true })),
  pin: vi.fn(async () => ({ ok: true })),
  remove: vi.fn(async () => ({ ok: true })),
}))

const copilotMock = vi.hoisted(() => ({
  getCopilotMetrics: vi.fn(async () => ({
    days: 7, runs: 0, ok_rate: null, avg_tools: null, p95_elapsed_ms: null, by_recipe: [],
  })),
  getPendingActions: vi.fn(async () => [] as unknown[]),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),
  getCopilotPresets: vi.fn(async () => []),
  getCopilotBrief: vi.fn(async () => null),
  getCopilotMentions: vi.fn(async () => ({ stocks: [], agents: [] })),
}))

vi.mock('@sida/api', () => ({ chatApi: apiMock }))
vi.mock('../../src/lib/copilot/api', () => ({
  getCopilotMetrics: copilotMock.getCopilotMetrics,
  getPendingActions: copilotMock.getPendingActions,
  applyPendingAction: copilotMock.applyPendingAction,
  rejectPendingAction: copilotMock.rejectPendingAction,
  reportViewState: copilotMock.reportViewState,
  getCopilotPresets: copilotMock.getCopilotPresets,
  getCopilotBrief: copilotMock.getCopilotBrief,
  getCopilotMentions: copilotMock.getCopilotMentions,
}))
vi.mock('../../src/hooks/useConversations', () => ({ useConversations: () => convMock }))
vi.mock('../../src/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md-fallback">{children}</div>,
}))
vi.mock('../../src/components/copilot/CopilotBriefCard', () => ({
  CopilotBriefCard: () => <div data-testid="brief-card" />,
}))

import AiCopilotPage from '../../src/pages/AiCopilot'

/** 一条待确认的「加自选」。 */
function row(id = 7, symbol = '600111') {
  return { id, kind: 'watchlist' as const, payload: { symbol }, status: 'pending', result: null, created_at: '' }
}

/** unhandled rejection 必须为空 —— 这正是验收里控制台报的那条。 */
let rejections: unknown[] = []
const onRejection = (reason: unknown) => rejections.push(reason)

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  rejections = []
  process.on('unhandledRejection', onRejection)
  apiMock.sendMessageStream.mockReset()
  apiMock.createConversation.mockReset()
  window.history.pushState({}, '', '/ai-copilot')
  for (const fn of Object.values(copilotMock)) (fn as ReturnType<typeof vi.fn>).mockClear?.()
  copilotMock.getPendingActions.mockImplementation(async () => [row()])
  copilotMock.applyPendingAction.mockImplementation(async () => ({}))
  copilotMock.rejectPendingAction.mockImplementation(async () => ({}))
})

afterEach(() => {
  process.off('unhandledRejection', onRejection)
  cleanup()
})

async function mount() {
  render(<AiCopilotPage />)
  await waitFor(() => expect(screen.getByTestId('copilot-pending-actions')).toBeTruthy())
}

const confirmButtons = () => screen.getAllByRole('button', { name: '确认' }) as HTMLButtonElement[]
const ignoreButtons = () => screen.getAllByRole('button', { name: '忽略' }) as HTMLButtonElement[]

describe('待确认动作 · 提交中禁重 + 失败就地提示', () => {
  it('连点两次「确认」只发一次请求，在途期间按钮禁用', async () => {
    let settle: () => void = () => {}
    copilotMock.applyPendingAction.mockImplementation(
      () => new Promise<void>((resolve) => { settle = () => resolve() }) as never,
    )
    await mount()

    const btn = confirmButtons()[0]
    await act(async () => {
      fireEvent.click(btn)
      fireEvent.click(btn) // 同一个事件循环里的第二次点击（DOM 还没重渲染）
    })

    expect(copilotMock.applyPendingAction).toHaveBeenCalledTimes(1)
    // 展示态也必须是「提交中」：按钮禁用 + aria-busy，用户看得出还在跑
    expect(confirmButtons()[0].disabled).toBe(true)
    expect(confirmButtons()[0].getAttribute('aria-busy')).toBe('true')
    expect(ignoreButtons()[0].disabled).toBe(true)

    await act(async () => {
      settle()
      await Promise.resolve()
    })
    expect(rejections).toEqual([])
  })

  it('确认失败：就地可见报错、不产生 unhandled rejection、待确认行原样保留且可重试', async () => {
    copilotMock.applyPendingAction.mockRejectedValue(new Error('HTTP 500'))
    await mount()

    await act(async () => {
      fireEvent.click(confirmButtons()[0])
    })

    const alert = await waitFor(() => screen.getByTestId('copilot-pending-error'))
    expect(alert.textContent).toMatch(/失败/)
    expect(alert.textContent).toMatch(/600111/)
    // 行还在（失败 = 没落库），按钮回到可点 —— 人能直接重试
    expect(screen.getByTestId('copilot-pending-actions')).toBeTruthy()
    expect(screen.getByText('600111')).toBeTruthy()
    expect(confirmButtons()[0].disabled).toBe(false)
    expect(confirmButtons()[0].getAttribute('aria-busy')).toBeNull()
    expect(rejections).toEqual([])
  })

  it('忽略失败同样就地报错，不静默吞掉', async () => {
    copilotMock.rejectPendingAction.mockRejectedValue(new Error('HTTP 502'))
    await mount()

    await act(async () => {
      fireEvent.click(ignoreButtons()[0])
    })

    const alert = await waitFor(() => screen.getByTestId('copilot-pending-error'))
    expect(alert.textContent).toMatch(/失败/)
    expect(screen.getByTestId('copilot-pending-actions')).toBeTruthy()
    expect(rejections).toEqual([])
  })

  it('确认成功：刷新待确认列表（后端已不再返回 → 整条消失）', async () => {
    let rows: unknown[] = [row()]
    copilotMock.getPendingActions.mockImplementation(async () => rows)
    copilotMock.applyPendingAction.mockImplementation(async () => {
      rows = []
      return {}
    })
    await mount()

    await act(async () => {
      fireEvent.click(confirmButtons()[0])
    })

    await waitFor(() => expect(screen.queryByTestId('copilot-pending-actions')).toBeNull())
    expect(copilotMock.getPendingActions.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(rejections).toEqual([])
  })
})
