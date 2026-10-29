// @vitest-environment jsdom
/**
 * 数智参谋空态（2026-10-29 高级化重排）。
 *
 * 背景：上一版空态是 2x2 卡片网格，第 4 格塞"最近一次运行"，
 * 没有运行记录时它是一张**死入口**（写着"还没有运行记录"却不可点）；
 * 同时"过程"面板里已经有同一份运行记录 —— 同一件事给了两个入口。
 *
 * 本文件钉住重排后的四件事：
 *  1. 空态只有**一个**引导区：一个形象 + 一句提示（不并列第二套入口）；
 *  2. 示例恰好三条，点一条直接提问（原有交互不变）；
 *  3. 没有运行记录时**不留死入口**（"最近一次"整块不渲染）；
 *  4. 有记录时只给一条真实回链（一条，不是卡片格里的一格）。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn(),
  createConversation: vi.fn().mockResolvedValue({ id: 11 }),
}))

const runsMock = vi.hoisted(() => ({
  ok: true,
  runs: [] as Array<Record<string, unknown>>,
  details: {},
  loading: false,
  detailLoading: false,
  error: null,
  loadDetail: vi.fn(async () => ({ ok: true, data: {} })),
  reload: vi.fn(),
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
vi.mock('../../src/hooks/useCopilotRuns', () => ({ useCopilotRuns: () => runsMock }))
vi.mock('../../src/components/AssistantAvatar', () => ({
  AssistantAvatar: ({ label, busy }: { label?: string; busy?: boolean }) => (
    <span data-testid="bot-avatar" data-label={label} data-busy={busy === undefined ? undefined : String(busy)} />
  ),
}))

import AiCopilotPage from '../../src/pages/AiCopilot'

const HINT = '从一个标的或技术问题开始，分析会保留数据来源与依据'

beforeEach(() => {
  vi.clearAllMocks()
  runsMock.runs = []
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  window.history.pushState({}, '', '/ai-copilot')
})

afterEach(cleanup)

async function renderEmpty() {
  render(<AiCopilotPage />)
  await waitFor(() => expect(screen.getByTestId('copilot-empty-hero')).toBeTruthy())
}

describe('参谋空态', () => {
  it('只有一个引导区：一个形象 + 一句提示，不并列第二套入口', async () => {
    await renderEmpty()
    expect(screen.getAllByTestId('copilot-empty-hero').length).toBe(1)
    // 形象唯一：空态里不放第二个助手形象抢焦点
    expect(screen.getAllByTestId('bot-avatar').length).toBe(1)
    expect(screen.getAllByTestId('bot-avatar')[0].getAttribute('data-label')).toBe('数智参谋')
    // 提示只出现一次（历史上出现过两套引导并存）
    expect(screen.getAllByText(HINT).length).toBe(1)
  })

  it('空态形象不带任何"正在思考"的假状态', async () => {
    await renderEmpty()
    expect(screen.getByTestId('bot-avatar').getAttribute('data-busy')).toBeNull()
  })

  it('示例恰好三条，点一条直接提问', async () => {
    await renderEmpty()
    const examples = screen.getAllByTestId('copilot-empty-example')
    expect(examples.length).toBe(3)
    // 三条都在同一个有序列表里：窄屏竖排、宽屏并排，不靠第 4 格凑满格子
    expect(screen.getAllByTestId('copilot-empty-examples').length).toBe(1)
    expect(screen.getByTestId('copilot-empty-examples').tagName).toBe('OL')
    apiMock.sendMessageStream.mockResolvedValue(undefined)
    fireEvent.click(screen.getByRole('button', { name: /集合竞价的量价结构/ }))
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalled())
  })

  it('没有运行记录时不渲染"最近一次"（死入口不再占位）', async () => {
    await renderEmpty()
    expect(screen.queryByTestId('copilot-empty-last-run')).toBeNull()
    expect(screen.queryByText('还没有运行记录')).toBeNull()
  })

  it('有运行记录时只给一条真实回链', async () => {
    runsMock.runs = [{ run_id: 'run-9', question: '看看 600111 的筹码', status: 'done', tool_calls: 3 }]
    await renderEmpty()
    const link = screen.getByTestId('copilot-empty-last-run')
    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe('/copilot/runs/run-9')
    expect(link.textContent).toContain('看看 600111 的筹码')
    expect(link.textContent).toContain('3 次工具')
    expect(screen.getAllByTestId('copilot-empty-last-run').length).toBe(1)
  })
})
