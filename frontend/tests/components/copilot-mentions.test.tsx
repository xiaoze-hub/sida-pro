// @vitest-environment jsdom
/**
 * D2 验收：`@` 提及面板（股票 / 内置智能体）。
 *
 * 四条必须成立的：面板能开、拼音能搜、键盘能选、输入法组合期间不弹。
 * 外加一条：选中的提及要真的进请求体（否则只是"看着好看"）。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn(async () => {}),
  createConversation: vi.fn().mockResolvedValue({ id: 21 }),
}))

const copilotMock = vi.hoisted(() => ({
  getCopilotMetrics: vi.fn(async () => ({ days: 7, runs: 0, ok_rate: null, avg_tools: null, p95_elapsed_ms: null, by_recipe: [] })),
  getPendingActions: vi.fn(async () => []),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),
  getCopilotPresets: vi.fn(async () => []),
  getCopilotBrief: vi.fn().mockResolvedValue(null),
  getCopilotMentions: vi.fn(async () => ({
    stocks: [
      { type: 'stock', symbol: '600111', name: '北方稀土', market: 'CN', py: 'bfxt' },
      { type: 'stock', symbol: '002361', name: '神剑股份', market: 'CN', py: 'sjgf' },
    ],
    agents: [
      {
        type: 'agent', id: 'postmarket_reviewer', label: '盘后复盘官', icon: '📊',
        desc: '收盘复盘：涨停梯队、炸板率', when: ['复盘'], tool_count: 6,
      },
      {
        type: 'agent', id: 'risk_judge', label: '风控裁判', icon: '🛡',
        desc: '持仓体检：回撤、破位', when: ['风控'], tool_count: 6,
      },
    ],
  })),
}))

vi.mock('@panwatch/api', () => ({ chatApi: apiMock }))
vi.mock('../../src/lib/copilot/api', () => copilotMock)
vi.mock('../../src/lib/copilot/symbolChips', () => ({
  extractSymbolChips: () => [],
  marketOf: (c: string) => (c.startsWith('6') ? 'SH' : 'SZ'),
}))
vi.mock('../../src/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md">{children}</div>,
}))
vi.mock('../../src/components/copilot/CopilotBriefCard', () => ({
  CopilotBriefCard: () => <div data-testid="brief-card" />,
}))

import AiCopilotPage from '../../src/pages/AiCopilot'

function typeIn(el: HTMLTextAreaElement, value: string) {
  // React 受控组件：必须走原生 setter，否则 tracker 认为值没变，onChange 不触发
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set
  setter?.call(el, value)
  el.setSelectionRange(value.length, value.length)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  apiMock.sendMessageStream.mockClear()
  copilotMock.getCopilotMentions.mockClear()
  window.history.pushState({}, '', '/ai-copilot')
})

afterEach(cleanup)

describe('@ 提及', () => {
  it('打 @ 弹出面板，默认股票 tab', async () => {
    render(<AiCopilotPage />)
    const ta = screen.getByPlaceholderText(/问筹码/) as HTMLTextAreaElement
    typeIn(ta, '@')

    const panel = await screen.findByTestId('copilot-mention-panel')
    expect(panel).toBeTruthy()
    expect(screen.getByTestId('copilot-mention-tab-stock')).toBeTruthy()
    expect(await screen.findByText('北方稀土')).toBeTruthy()
  })

  it('拼音首字母能搜到（bfxt → 北方稀土）', async () => {
    render(<AiCopilotPage />)
    const ta = screen.getByPlaceholderText(/问筹码/) as HTMLTextAreaElement
    typeIn(ta, '@bfxt')

    await waitFor(() => expect(screen.getByText('北方稀土')).toBeTruthy())
    expect(screen.queryByText('神剑股份')).toBeNull()
  })

  it('键盘 ↓ Enter 选中股票 → chip 出现，发送时进请求体', async () => {
    render(<AiCopilotPage />)
    const ta = screen.getByPlaceholderText(/问筹码/) as HTMLTextAreaElement
    typeIn(ta, '@bfxt')
    await screen.findByText('北方稀土')

    fireEvent.keyDown(ta, { key: 'Enter' })
    await waitFor(() => expect(screen.getByTestId('copilot-mention-chips')).toBeTruthy())

    fireEvent.click(screen.getByLabelText('发送'))
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalled())
    const call = apiMock.sendMessageStream.mock.calls[0]
    expect(call[5]).toEqual({ symbol: '600111', market: 'SH' })
    expect(call[6]).toEqual([{ type: 'stock', value: '600111', label: '北方稀土' }])
  })

  it('切到智能体 tab 并选中 → 请求体带 agent recipe', async () => {
    render(<AiCopilotPage />)
    const ta = screen.getByPlaceholderText(/问筹码/) as HTMLTextAreaElement
    typeIn(ta, '@')
    await screen.findByTestId('copilot-mention-panel')

    fireEvent.click(screen.getByTestId('copilot-mention-tab-agent'))
    fireEvent.click(await screen.findByText('盘后复盘官'))

    fireEvent.click(screen.getByLabelText('发送'))
    await waitFor(() => expect(apiMock.sendMessageStream).toHaveBeenCalled())
    expect(apiMock.sendMessageStream.mock.calls[0][6]).toEqual([
      { type: 'agent', value: 'postmarket_reviewer', label: '盘后复盘官' },
    ])
  })

  it('全角 ＠（中文输入法默认产出）也能弹面板', async () => {
    render(<AiCopilotPage />)
    const ta = screen.getByPlaceholderText(/问筹码/) as HTMLTextAreaElement
    typeIn(ta, '\uFF20')

    expect(await screen.findByTestId('copilot-mention-panel')).toBeTruthy()
    expect(await screen.findByText('北方稀土')).toBeTruthy()
  })

  it('输入法组合结束补检测：不再需要多敲一个字符', async () => {
    render(<AiCopilotPage />)
    const ta = screen.getByPlaceholderText(/问筹码/) as HTMLTextAreaElement

    fireEvent.compositionStart(ta)
    typeIn(ta, '@北方')
    await new Promise((r) => setTimeout(r, 120))
    expect(screen.queryByTestId('copilot-mention-panel')).toBeNull()

    // 关键：compositionEnd 之后**不再有任何输入事件**
    fireEvent.compositionEnd(ta)
    await waitFor(() => expect(screen.getByTestId('copilot-mention-panel')).toBeTruthy())
  })

  it('中文输入法组合期间不弹面板', async () => {
    render(<AiCopilotPage />)
    const ta = screen.getByPlaceholderText(/问筹码/) as HTMLTextAreaElement

    fireEvent.compositionStart(ta)
    typeIn(ta, '@bf')
    await new Promise((r) => setTimeout(r, 260))
    expect(screen.queryByTestId('copilot-mention-panel')).toBeNull()

    fireEvent.compositionEnd(ta)
    // 值必须变化，否则 React 认为没改动、onChange 不会再触发
    typeIn(ta, '@bfxt')
    await waitFor(() => expect(screen.getByTestId('copilot-mention-panel')).toBeTruthy())
  })
})
