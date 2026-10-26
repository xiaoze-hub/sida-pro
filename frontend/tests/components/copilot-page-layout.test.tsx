// @vitest-environment jsdom
/**
 * 数智参谋页 P0/P1 验收：
 *  - `?symbol=` 上下文带入（显示「正在看」锚点并出参谋卡）；
 *  - 指令池分层（今日推荐 + 更多，可展开）；
 *  - 回答按【结论】/【依据】渲染成卡片，依据可展开、带追问 chips；
 *  - 非模板回答回退 markdown 原文（不吃掉回答）。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn(),
  createConversation: vi.fn().mockResolvedValue({ id: 11 }),
}))

const presetsMock = vi.hoisted(() => ({
  getCopilotPresets: vi.fn(),
  getCopilotBrief: vi.fn().mockResolvedValue(null),
}))

vi.mock('@panwatch/api', () => ({ chatApi: apiMock }))
vi.mock('../../src/lib/copilot/api', () => ({
  getCopilotMetrics: vi.fn(async () => ({ days: 7, runs: 0, ok_rate: null, avg_tools: null, p95_elapsed_ms: null, by_recipe: [] })),

  getPendingActions: vi.fn(async () => []),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),

  ...presetsMock,
  getCopilotMentions: vi.fn(async () => ({ stocks: [], agents: [] })),
}))
vi.mock('../../src/lib/copilot/symbolChips', () => ({
  extractSymbolChips: (text: string) => {
    const m = text.match(/\b([034689]\d{5})\b/)
    return m ? [m[1]] : []
  },
  marketOf: (code: string) =>
    code.startsWith('6') ? 'SH' : /^[489]/.test(code) ? 'BJ' : 'SZ',
}))
vi.mock('../../src/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md-fallback">{children}</div>,
}))
vi.mock('../../src/components/AssistantAvatar', () => ({
  AssistantAvatar: ({ busy = false }: { busy?: boolean }) => <span data-testid="bot-avatar" data-busy={String(busy)} />,
}))
vi.mock('../../src/components/copilot/CopilotBriefCard', () => ({
  CopilotBriefCard: ({ symbol }: { symbol: string }) => <div data-testid="brief-card">{symbol}</div>,
}))

import AiCopilotPage from '../../src/pages/AiCopilot'

const PRESETS = [
  { id: 'full_dossier', icon: '🧬', label: '全景数智深度诊断', prompt: '全面分析', group: 'dossier', requires_perm: null },
  { id: 'auction_strong', icon: '🔥', label: '扫描竞价抢筹', prompt: '扫竞价', group: 'auction', requires_perm: null },
  { id: 'chip_cost', icon: '🎯', label: '底仓成本诊断', prompt: '看底仓', group: 'chip', requires_perm: null },
  { id: 'resonance_first', icon: '🛡️', label: '三指标共振', prompt: '筛共振', group: 'resonance', requires_perm: null },
  { id: 'demon_mainline', icon: '👑', label: '妖股候选', prompt: '找妖股', group: 'demon', requires_perm: null },
]

const STRUCTURED = `【结论】洗盘吸筹偏多，短期震荡消化，置信度 6/10
【依据】
- 逐笔主力净流出 552 万
- 底仓成本 40.16 元
- 获利盘 8.3%
- GS 处于 S 区
【预案】持仓者：跌破 33.96 元后重新评估
【口径】腾讯逐笔 + 自算CYQ
以上分析仅供参考，不构成投资建议。`

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  presetsMock.getCopilotPresets.mockResolvedValue(PRESETS)
  apiMock.sendMessageStream.mockReset()
  window.history.pushState({}, '', '/ai-copilot')
})

afterEach(cleanup)

describe('数智参谋 P0/P1', () => {
  it('退出助手时中止当前回复流，避免跨账号保留请求', async () => {
    let finish!: () => void
    let signal: AbortSignal | undefined
    apiMock.sendMessageStream.mockImplementation((_id, _content, _handlers, streamSignal) => {
      signal = streamSignal
      return new Promise<void>(resolve => { finish = resolve })
    })
    const view = render(<AiCopilotPage />)
    fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: '检查界面状态' } })
    fireEvent.click(screen.getByLabelText('发送'))
    await waitFor(() => expect(signal).toBeTruthy())
    view.unmount()
    expect(signal?.aborted).toBe(true)
    await act(async () => finish())
  })

  it('仅最后一个 AI 气泡显示形象，等待与流式书写时提速，完成后回到空闲态', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id, _content, handlers) => { handlers.onDelta('首条界面测试说明。') })
    const view = render(<AiCopilotPage />)
    const input = screen.getByPlaceholderText(/问点什么|补充说明/)
    fireEvent.change(input, { target: { value: '测试首条说明' } })
    fireEvent.click(screen.getByLabelText('发送'))
    await waitFor(() => expect(view.container.querySelector('[data-assistant-bubble="latest"] [data-busy="false"]')).toBeTruthy())
    let finish!: () => void
    let delta!: (text: string) => void
    apiMock.sendMessageStream.mockImplementation((_id, _content, handlers) => new Promise<void>(resolve => { finish = resolve; delta = handlers.onDelta }))
    fireEvent.change(input, { target: { value: '再次说明' } })
    fireEvent.click(screen.getByLabelText('发送'))
    await waitFor(() => expect(view.container.querySelector('[data-assistant-bubble="pending"] [data-busy="true"]')).toBeTruthy())
    expect(view.container.querySelector('[data-assistant-bubble="history"] [data-testid="bot-avatar"]')).toBeNull()
    await waitFor(() => expect(delta).toBeTypeOf('function'))
    act(() => delta('正在书写新的界面测试说明。'))
    await waitFor(() => expect(view.container.querySelector('[data-assistant-bubble="latest"] [data-busy="true"]')).toBeTruthy())
    expect(view.container.querySelector('[data-assistant-bubble="pending"]')).toBeNull()
    await act(async () => finish())
    await waitFor(() => expect(view.container.querySelector('[data-assistant-bubble="latest"] [data-busy="false"]')).toBeTruthy())
    expect(view.container.querySelectorAll('[data-assistant-bubble] [data-testid="bot-avatar"]').length).toBe(1)
  })
  it('?symbol= 只带入锚点，不自动拉参谋卡（brief 接口重，避免进页面就卡 10s+）', async () => {
    window.history.pushState({}, '', '/ai-copilot?symbol=600111')
    render(<AiCopilotPage />)

    await waitFor(() => expect(screen.getByTestId('copilot-context-anchor')).toBeTruthy())
    expect(screen.getByTestId('copilot-context-anchor').textContent).toContain('600111')
    // 不主动请求参谋卡
    expect(screen.queryByTestId('brief-card')).toBeNull()

    // 2026-10-26: 「代码输入 + 看卡」已按用户指令移除（与主输入框重复）
    expect(screen.queryByText('看卡')).toBeNull()
    expect(screen.queryByTestId('brief-card')).toBeNull()
  })

  it('预设入口已整体移除（用户裁定不需要）', async () => {
    render(<AiCopilotPage />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    // 预设卡片、预设"更多"折叠、移动端预设条都不应存在
    expect(screen.queryByTestId('copilot-preset-more')).toBeNull()
    expect(screen.queryByTestId('copilot-mobile-chip')).toBeNull()
    expect(screen.queryByText('全景数智深度诊断')).toBeNull()
  })

  it('结构化回答渲染成卡片：置信度、依据折叠、追问 chips', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id, _c, handlers) => {
      handlers.onDelta(STRUCTURED)
    })
    render(<AiCopilotPage />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())

    fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: '600111 怎么样' } })
    fireEvent.click(screen.getByLabelText('发送'))

    await waitFor(() => expect(screen.getByTestId('copilot-answer-card')).toBeTruthy())
    expect(screen.getByTestId('copilot-answer-confidence').textContent).toContain('6/10')
    // 默认只露 3 条，第 4 条折叠
    expect(screen.getByTestId('copilot-answer-expand').textContent).toContain('1')
    expect(screen.queryByText(/GS 处于 S 区/)).toBeNull()

    fireEvent.click(screen.getByTestId('copilot-answer-expand'))
    await waitFor(() => expect(screen.getByText(/GS 处于 S 区/)).toBeTruthy())

    // 追问 chips 存在，点了会发出去
    fireEvent.click(screen.getByText('上方套牢筹码集中在哪个价位'))
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalledTimes(2))
    const lastCall = apiMock.sendMessageStream.mock.calls[1]
    expect(lastCall[1]).toBe('上方套牢筹码集中在哪个价位')
  })

  it('非模板回答回退 markdown，不丢内容', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id, _c, handlers) => {
      handlers.onDelta('今天两市成交 1.2 万亿，情绪回暖。')
    })
    render(<AiCopilotPage />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())

    fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: '今天市场怎么样' } })
    fireEvent.click(screen.getByLabelText('发送'))

    await waitFor(() => expect(screen.getByTestId('md-fallback')).toBeTruthy())
    expect(screen.getByTestId('md-fallback').textContent).toContain('1.2 万亿')
    expect(screen.queryByTestId('copilot-answer-card')).toBeNull()
  })
})

describe('标的锁定（2026-10-19 修复：看 600111 却分析成 002361）', () => {
  it('点推荐指令时把锚点标的随请求发出，并绑定到会话', async () => {
    window.history.pushState({}, '', '/ai-copilot?symbol=600111')
    apiMock.sendMessageStream.mockImplementation(async () => {})
    render(<AiCopilotPage />)

    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: '全面分析' } })
    fireEvent.click(screen.getByLabelText('发送'))

    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalled())
    const [, prompt, , , , stock] = apiMock.sendMessageStream.mock.calls[0]
    expect(prompt).toBe('全面分析')
    expect(stock).toEqual({ symbol: '600111', market: 'SH' })
    expect(apiMock.createConversation).toHaveBeenCalledWith(
      expect.objectContaining({ stock_symbol: '600111', stock_market: 'SH' }),
    )
  })

  it('文字里写了代码时以文字为准，不被旧锚点覆盖', async () => {
    window.history.pushState({}, '', '/ai-copilot?symbol=600111')
    apiMock.sendMessageStream.mockImplementation(async () => {})
    render(<AiCopilotPage />)

    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: '002361 怎么样' } })
    fireEvent.click(screen.getByLabelText('发送'))

    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalled())
    const [, , , , , stock] = apiMock.sendMessageStream.mock.calls[0]
    expect(stock).toEqual({ symbol: '002361', market: 'SZ' })
  })
})
