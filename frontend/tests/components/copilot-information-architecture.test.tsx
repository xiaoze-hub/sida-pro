// @vitest-environment jsdom
/**
 * 参谋页信息架构（2026-10-26 重构后）。
 *
 * 背景：页面一度被切成 4 条竖带（全局图标栏 + 预设 330 + 会话 350 + 主区），
 * 主区中下部一整块空白、顶部挤 6 个并列小按钮。用户当场指出"不觉得很丑吗，分三块"。
 *
 * 本文件钉住重构后的三件事：
 *  1. **会话列表不再单独占一条竖带**：并入左侧栏的「对话」tab；
 *  2. **顶部只留高频一个（过程）+ 更多**，记忆/定时/提案收进下拉；
 *  3. **空态有承接**：没有消息时主区给引导块，不是一整块空白。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn(),
  createConversation: vi.fn().mockResolvedValue({ id: 11 }),
}))

vi.mock('@panwatch/api', () => ({ chatApi: apiMock }))
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

import AiCopilot from '@/pages/AiCopilot'

beforeEach(() => {
  vi.clearAllMocks()
  // jsdom 没有 scrollTo（自动滚动到最新消息会用到）
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
})

afterEach(cleanup)

async function renderPage() {
  render(<AiCopilot />)
  await waitFor(() => expect(screen.getByTestId('ai-copilot-page')).toBeTruthy())
}

describe('参谋页信息架构', () => {
  it('会话列表并入左侧栏 tab，不再单独占一条竖带', async () => {
    await renderPage()
    // 原来那条独立会话列必须消失
    expect(screen.queryByTestId('copilot-conversation-column')).toBeNull()
    // 左侧栏是两个 tab
    expect(screen.getByTestId('left-tab-presets')).toBeTruthy()
    expect(screen.getByTestId('left-tab-conversations')).toBeTruthy()
  })

  it('切到「对话」tab 才渲染会话列表', async () => {
    await renderPage()
    expect(screen.queryByTestId('left-conversation-list')).toBeNull()
    fireEvent.click(screen.getByTestId('left-tab-conversations'))
    await waitFor(() => expect(screen.getByTestId('left-conversation-list')).toBeTruthy())
  })

  it('顶部只留「过程」，记忆/定时/提案收进「更多」', async () => {
    await renderPage()
    expect(screen.getByTestId('copilot-panel-runs')).toBeTruthy()
    expect(screen.getByTestId('copilot-more-panels')).toBeTruthy()
    // 三个低频页签在下拉里仍然可达（details 内容不隐藏于 DOM）
    expect(screen.getByTestId('copilot-panel-memory')).toBeTruthy()
    expect(screen.getByTestId('copilot-panel-schedule')).toBeTruthy()
    expect(screen.getByTestId('copilot-panel-proposals')).toBeTruthy()
  })

  it('空态只有一套引导（不重复），点示例填入输入框', async () => {
    await renderPage()
    // 页面本来就有 PAGE_EMPTY_HINT + PAGE_EMPTY_EXAMPLES；我在 v0.78 又加了一套，被移除。
    // 现在钉住：hint 恰好出现一次，示例按钮恰好三个。
    await waitFor(() => expect(screen.getByText('也可以直接点一条，不用自己组织语言')).toBeTruthy())
    expect(screen.getAllByText('也可以直接点一条，不用自己组织语言').length).toBe(1)
    // 原有引导的交互是"点一条直接问"（不是填入输入框），钉住这个行为
    expect(screen.getAllByTestId('copilot-empty-example').length).toBe(3)
    apiMock.sendMessageStream.mockResolvedValue(undefined)
    fireEvent.click(screen.getByRole('button', { name: /爆量抢筹/ }))
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalled())
  })
})
