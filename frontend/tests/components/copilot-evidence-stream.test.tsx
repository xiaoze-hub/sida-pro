// @vitest-environment jsdom
/**
 * 参谋页 · 证据齐套度条 + 价格锚定接线回归（2026-10-23）。
 *
 * 前面两个纯函数/组件用例证明「零件对」，这里证明**真的接上了**：
 *  ① SSE 的 stage 事件（`正在取数 n/N：get_xxx`）一进来，齐套度条就出现，
 *     并且是本轮的真状态（前两条 ✓、最后一条 ⏳），不是一条静态假进度；
 *  ② 回答定稿后条子**不消失**，固化在回答顶部（可回看「这份结论取到了哪几路证据」）；
 *  ③ 回答里的价位 chip 点下去，页面出「K线锚点」回执并带工作台链接
 *     （K 线画线由别的模块拥有，本页只出可见回执，不假装图已经动了）。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn(),
  createConversation: vi.fn().mockResolvedValue({ id: 21 }),
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
  marketOf: (code: string) => (code.startsWith('6') ? 'SH' : 'SZ'),
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
]

const STRUCTURED = `【结论】洗盘吸筹偏多，站稳支撑 32.80 后再看，置信度 6/10
【依据】
- 逐笔主力净流出 552 万
- 底仓成本 40.16 元
- 今日 -1.9%
【预案】持仓者：跌破 33.20 元后重新评估
【口径】腾讯逐笔 + 自算CYQ
以上分析仅供参考，不构成投资建议。`

const STAGES = [
  '正在取数 1/3：get_stock_quote',
  '正在取数 2/3：get_chip_accumulation',
  '正在取数 3/3：get_delta_series',
]

/** 挂住流：测试先断言「流中」的样子，再放行出正文 */
let releaseStream: (() => void) | null = null

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  presetsMock.getCopilotPresets.mockResolvedValue(PRESETS)
  apiMock.sendMessageStream.mockReset()
  releaseStream = null
  window.history.pushState({}, '', '/ai-copilot?symbol=600111')
})

afterEach(cleanup)

describe('证据齐套度条接线', () => {
  it('流中按 stage 出 pill（✓/⏳），定稿后仍然留在回答顶部', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _c: string, handlers: any) => {
      for (const s of STAGES) handlers.onStage(s)
      await new Promise<void>((resolve) => {
        releaseStream = resolve
      })
      handlers.onDelta(STRUCTURED)
    })

    render(<AiCopilotPage />)
    await screen.findByText('全景数智深度诊断')

    fireEvent.change(screen.getByPlaceholderText(/问筹码|补充说明/), { target: { value: '600111 怎么样' } })
    fireEvent.click(screen.getByLabelText('发送'))

    // 流中：三个 pill，前两条已到手，最后一条在跑
    await waitFor(() => expect(screen.getByTestId('evidence-readiness')).toBeTruthy())
    const pills = screen.getAllByTestId('evidence-readiness-item')
    expect(pills.map((p) => p.getAttribute('data-key'))).toEqual([
      'get_stock_quote',
      'get_chip_accumulation',
      'get_delta_series',
    ])
    expect(pills.map((p) => p.getAttribute('data-status'))).toEqual(['ok', 'ok', 'pending'])
    expect(pills[2].textContent).toContain('⏳')
    expect(screen.getByTestId('evidence-readiness').textContent).toContain('已取 2/3')
    // stage 原文里能认出工具时，不再并排一条单调的「正在取数」文案
    expect(screen.queryByText(/正在取数 3\/3/)).toBeNull()

    // 放行正文 → 回答落屏，条子固化（pending 结算成 ok），不是消失
    await act(async () => {
      releaseStream?.()
    })
    await waitFor(() => expect(screen.getByTestId('copilot-answer-card')).toBeTruthy())
    await waitFor(() =>
      expect(screen.getByTestId('evidence-readiness').textContent).toContain('已取 3/3'),
    )
    expect(screen.getAllByTestId('evidence-readiness-item')).toHaveLength(3)
  })

  it('认不出的 stage（散文提示）仍走 spinner，不污染齐套度', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _c: string, handlers: any) => {
      handlers.onStage('正在准备上下文…')
    })

    render(<AiCopilotPage />)
    await screen.findByText('全景数智深度诊断')
    fireEvent.change(screen.getByPlaceholderText(/问筹码|补充说明/), { target: { value: '看看' } })
    fireEvent.click(screen.getByLabelText('发送'))

    await waitFor(() => expect(screen.getByText('正在准备上下文…')).toBeTruthy())
    expect(screen.queryByTestId('evidence-readiness')).toBeNull()
  })
})

describe('回答内价格锚定接线', () => {
  it('点价位 chip → 出「K线锚点」回执，带标的与工作台链接', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _c: string, handlers: any) => {
      handlers.onStage('正在取数 1/1：get_stock_quote')
      handlers.onDelta(STRUCTURED)
    })

    render(<AiCopilotPage />)
    await screen.findByText('全景数智深度诊断')
    fireEvent.change(screen.getByPlaceholderText(/问筹码|补充说明/), { target: { value: '600111 怎么样' } })
    fireEvent.click(screen.getByLabelText('发送'))

    await waitFor(() => expect(screen.getByTestId('copilot-answer-card')).toBeTruthy())
    const chips = screen.getAllByTestId('answer-price-anchor')
    expect(chips.length).toBeGreaterThan(0)

    // 32.80 在裁决句里（支撑）
    fireEvent.click(chips[0])
    await waitFor(() => expect(screen.getByTestId('copilot-price-anchor')).toBeTruthy())
    const row = screen.getByTestId('copilot-price-anchor')
    expect(row.textContent).toContain('32.80')
    expect(row.textContent).toContain('支撑')
    expect(row.textContent).toContain('600111')

    // 清除回执
    fireEvent.click(screen.getByText('清除'))
    await waitFor(() => expect(screen.queryByTestId('copilot-price-anchor')).toBeNull())
  })
})
