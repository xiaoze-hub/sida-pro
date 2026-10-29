// @vitest-environment jsdom
/**
 * 个股工作台 → 数智参谋 · 图表上下文带入（P2, 2026-10-29）。
 *
 * 用**真实参谋页**（只 mock 网络层）证明一条完整链路：
 *   ① 进入参谋只是"带入"，不自动发问（createConversation / sendMessageStream 都不会自己跑）；
 *   ② 作战台上那张图的可证读数（类型/市场/周期/可视区间/已确认价位）以可见条形式出现在会话顶部；
 *   ③ 用户第一次发问时，这段上下文随 `initial_context` 真正送到后端（不是只在界面上写着）；
 *   ④ 用户清除 / 换标的 / 切到历史会话后，上下文立刻失效，且不会再被塞进请求；
 *   ⑤ 别的账号发布的上下文读不到（不串号）。
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
import { AssistantPageHost, AssistantWorkspaceProvider } from '../../src/components/AssistantWorkspace'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import {
  buildChartContext,
  chartContextStoreSize,
  clearChartContextStore,
  publishChartContext,
  readChartContext,
  type ChartContext,
} from '../../src/lib/copilot/chart-context'

const OWNER = 'test-user'

/** 工作台那张图的真实读数（形状照 `onRangeStats` / 服务端标注行）。 */
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

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: OWNER }))}.signature`)
  window.history.pushState({}, '', '/ai-copilot')
  clearChartContextStore()
  apiMock.createConversation.mockReset()
  apiMock.createConversation.mockResolvedValue({ id: 21 })
  apiMock.sendMessageStream.mockReset()
  apiMock.sendMessageStream.mockImplementation(async (_id: number, _q: string, handlers: any) => {
    handlers.onDone({ id: 7, role: 'assistant', content: '收到。', created_at: '' })
    return { content: '收到。', authoritative: true, corruptedFrames: 0, errors: [] }
  })
  convMock.list.mockClear()
})

afterEach(() => {
  cleanup()
  localStorage.removeItem('token')
  clearChartContextStore()
})

describe('带入 ≠ 自动发问', () => {
  it('进入参谋后只显示上下文条，一个请求都不替用户发', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    const chip = await screen.findByTestId('copilot-chart-context')
    expect(chip.textContent).toContain('日线')
    expect(apiMock.createConversation).not.toHaveBeenCalled()
    expect(apiMock.sendMessageStream).not.toHaveBeenCalled()
  })
})

describe('可见上下文条', () => {
  it('类型/市场/周期/可视区间/已确认价位逐项可见', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    const chip = await screen.findByTestId('copilot-chart-context')
    const text = chip.textContent ?? ''
    expect(text).toContain('600111')
    expect(text).toContain('个股')
    expect(text).toContain('CN')
    expect(text).toContain('日线')
    expect(text).toContain('2026-06-01→2026-10-29')
    expect(text).toContain('58 根')
    expect(text).toContain('+12.34%')
    expect(text).toContain('12.34')
    expect(text).toContain('15.20')
    expect(text).toContain('进入时快照')
  })

  it('工作台没上报区间/价位 → 只显示能证明的部分，不出现占位假值', async () => {
    publishChartContext(workbenchContext({ range: null, levels: null }))
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    const text = (await screen.findByTestId('copilot-chart-context')).textContent ?? ''
    expect(text).toContain('600111')
    expect(text).not.toContain('区间')
    expect(text).not.toContain('价位')
    expect(text).not.toContain('--')
  })

  it('工作台没有发布（没从工作台进） → 不渲染上下文条', async () => {
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    expect(screen.queryByTestId('copilot-chart-context')).toBeNull()
  })

  it('别的账号发布过的上下文读不到（不串号）', async () => {
    publishChartContext(workbenchContext({ owner: 'someone-else' }))
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    expect(screen.queryByTestId('copilot-chart-context')).toBeNull()
  })
})

describe('发问时才把上下文送进会话', () => {
  it('首次发问建会话时 initial_context 含图表上下文与"不自动下单/画线"纪律', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('这个区间的量能怎么看')
    await waitFor(() => expect(apiMock.createConversation).toHaveBeenCalled())
    const payload = apiMock.createConversation.mock.calls[0][0] as { stock_symbol: string; initial_context: string }
    expect(payload.stock_symbol).toBe('600111')
    expect(payload.initial_context).toContain('图表上下文')
    expect(payload.initial_context).toContain('2026-06-01')
    expect(payload.initial_context).toContain('58 根')
    expect(payload.initial_context).toContain('12.34')
    expect(payload.initial_context).toContain('不要主动分析')
    expect(payload.initial_context).toContain('自动下单')
    expect(payload.initial_context).toContain('自动画线')
  })

  it('用户清除后：条消失，下一次建会话不再带图表上下文', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    fireEvent.click(screen.getByTestId('copilot-chart-context-clear'))
    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())
    // 清除同时撤掉工作台的发布 —— 否则下次进参谋又自己复活
    expect(readChartContext('600111', 'CN', OWNER)).toBeNull()
    expect(chartContextStoreSize()).toBe(0)
    await ask('再看看')
    await waitFor(() => expect(apiMock.createConversation).toHaveBeenCalled())
    const payload = apiMock.createConversation.mock.calls[0][0] as { initial_context: string }
    expect(payload.initial_context).not.toContain('图表上下文')
  })

  it('用户改问另一只票 → 工作台那张图的上下文立刻失效，不跟着发出去', async () => {
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    await ask('000776 怎么样')
    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())
    await waitFor(() => expect(apiMock.createConversation).toHaveBeenCalled())
    const payload = apiMock.createConversation.mock.calls[0][0] as { stock_symbol: string; initial_context: string }
    expect(payload.stock_symbol).toBe('000776')
    expect(payload.initial_context).not.toContain('图表上下文')
  })

  it('切到历史会话 → 上下文不跟着串进别人的对话', async () => {
    apiMock.getConversation.mockResolvedValue({
      conversation: { id: 9, title: '旧对话', stock_symbol: null, created_at: '' },
      messages: [{ id: 1, role: 'user', content: '旧问题', created_at: '' }],
    })
    publishChartContext(workbenchContext())
    renderPage({ id: 1, symbol: '600111', market: 'CN' })
    await screen.findByTestId('copilot-chart-context')
    const item = await screen.findByText('旧对话')
    await act(async () => {
      fireEvent.click(item)
    })
    await waitFor(() => expect(screen.queryByTestId('copilot-chart-context')).toBeNull())
  })
})

describe('真实入口链路（路由 + 会话容器）', () => {
  it('派发 sida-open-chat（工作台胶囊在 App 里的真实做法）→ 参谋页带上那张图，且不替用户发问', async () => {
    publishChartContext(workbenchContext())
    render(
      <MemoryRouter initialEntries={['/ai-copilot']}>
        <AssistantWorkspaceProvider>
          <Routes>
            <Route path="/ai-copilot" element={<AssistantPageHost />} />
          </Routes>
        </AssistantWorkspaceProvider>
      </MemoryRouter>,
    )
    await waitFor(() => expect(document.querySelector('[data-assistant-page-host]')).toBeTruthy())
    await act(async () => {
      window.dispatchEvent(
        new CustomEvent('sida-open-chat', { detail: { symbol: '600111', market: 'CN' } }),
      )
    })
    const chip = await screen.findByTestId('copilot-chart-context')
    expect(chip.textContent).toContain('个股')
    expect(chip.textContent).toContain('日线')
    expect(chip.textContent).toContain('58 根')
    expect(chip.textContent).toContain('12.34')
    expect(apiMock.createConversation).not.toHaveBeenCalled()
    expect(apiMock.sendMessageStream).not.toHaveBeenCalled()
  })
})
