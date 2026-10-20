// @vitest-environment jsdom
/**
 * 数智参谋页 P0/P1 验收：
 *  - `?symbol=` 上下文带入（显示「正在看」锚点并出参谋卡）；
 *  - 指令池分层（今日推荐 + 更多，可展开）；
 *  - 回答按【结论】/【依据】渲染成卡片，依据可展开、带追问 chips；
 *  - 非模板回答回退 markdown 原文（不吃掉回答）。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  it('?symbol= 只带入锚点，不自动拉参谋卡（brief 接口重，避免进页面就卡 10s+）', async () => {
    window.history.pushState({}, '', '/ai-copilot?symbol=600111')
    render(<AiCopilotPage />)

    await waitFor(() => expect(screen.getByTestId('copilot-context-anchor')).toBeTruthy())
    expect(screen.getByTestId('copilot-context-anchor').textContent).toContain('600111')
    // 不主动请求参谋卡
    expect(screen.queryByTestId('brief-card')).toBeNull()

    // 点「看卡」才拉
    fireEvent.click(screen.getByText('看卡'))
    await waitFor(() => expect(screen.getByTestId('brief-card').textContent).toBe('600111'))
  })

  it('指令池分层：今日推荐 3 条 + 更多可展开', async () => {
    render(<AiCopilotPage />)
    await screen.findByText('全景数智深度诊断')

    const more = screen.getByTestId('copilot-preset-more')
    expect(more.textContent).toContain('更多指令')

    // 排序随时段变化（跑测试的时刻不同，推荐项也不同）→ 只断言「数量」不写死具体项
    const labels = PRESETS.map((p) => p.label)
    const visibleCount = () => labels.filter((l) => screen.queryByText(l)).length
    expect(visibleCount()).toBe(3)

    fireEvent.click(more)
    await waitFor(() => expect(visibleCount()).toBe(5))
  })

  it('结构化回答渲染成卡片：置信度、依据折叠、追问 chips', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id, _c, handlers) => {
      handlers.onDelta(STRUCTURED)
    })
    render(<AiCopilotPage />)
    await screen.findByText('全景数智深度诊断')

    fireEvent.change(screen.getByPlaceholderText(/问筹码|补充说明/), { target: { value: '600111 怎么样' } })
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
    await screen.findByText('全景数智深度诊断')

    fireEvent.change(screen.getByPlaceholderText(/问筹码|补充说明/), { target: { value: '今天市场怎么样' } })
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

    await screen.findByText('全景数智深度诊断')
    fireEvent.click(screen.getAllByText('全景数智深度诊断')[0])

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

    await screen.findByText('全景数智深度诊断')
    fireEvent.change(screen.getByPlaceholderText(/问筹码|补充说明/), { target: { value: '002361 怎么样' } })
    fireEvent.click(screen.getByLabelText('发送'))

    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalled())
    const [, , , , , stock] = apiMock.sendMessageStream.mock.calls[0]
    expect(stock).toEqual({ symbol: '002361', market: 'SZ' })
  })
})
