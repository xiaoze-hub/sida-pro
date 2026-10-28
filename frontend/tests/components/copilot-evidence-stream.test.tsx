// @vitest-environment jsdom
/**
 * 参谋页 · 证据齐套度条 + 价格锚定接线回归（2026-10-23，2026-10-28 按事件契约重写）。
 *
 * 前面两个纯函数/组件用例证明「零件对」，这里证明**真的接上了**：
 *  ① SSE 的 stage 事件（`正在取数 n/N：get_xxx`）一进来，齐套度条就出现，
 *     并且是本轮的真状态 —— 开跑帧只证明「开始取了」，不是「取到了」，
 *     所以流中三条全是 pending（⏳）、已取 0/3，前两条不许先变 ✓；
 *  ② 回答定稿后条子**不消失**，固化在回答顶部；流结束只把 pending 落成
 *     unknown（未确认），**不是** ok —— 已取仍是 0/3，没有一条伪成功；
 *  ③ 有失败证据的帧落 failed（未取到），有显式完成证据的帧才落 ✓；
 *  ④ 回答里的价位 chip 点下去，页面出「K线锚点」回执并带工作台链接
 *     （K 线画线由别的模块拥有，本页只出可见回执，不假装图已经动了）。
 *
 * 事件契约（2026-10-28 只读核对后端，基线 a6eb3c98）：
 *  - `src/harness/pipeline.py:425` / `pipeline_market.py:355` 只在工具**开跑**时
 *    yield 一条 stage，没有完成帧；
 *  - `src/web/api/chat.py:2260` 把它包成 `{"message": str}`，前端 onStage 只拿到字符串，
 *    没有 ok/status 字段（`packages/api/src/chat.ts:25`）；
 *  - 每个工具的真实成败只在收尾 `("runs", (results, errors))` 里，未下发前端。
 * 因此「下一条开跑 ⇒ 上一条完成」和「流结束 ⇒ 全部完成」都是无证据推断，本文件禁止回归。
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

vi.mock('@sida/api', () => ({ chatApi: apiMock }))
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
import { EvidenceReadinessStrip } from '../../src/components/copilot/EvidenceReadinessStrip'
import { foldStageEvents, settleReadiness } from '../../src/lib/copilot/evidence-readiness'

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

function typeAndSend(text: string) {
  const box = screen.getByPlaceholderText(/问点什么|补充说明/)
  fireEvent.change(box, { target: { value: text } })
  fireEvent.click(screen.getByLabelText('发送'))
}

describe('证据齐套度条接线', () => {
  it('流中三条全 pending（开跑 ≠ 取到）：已取 0/3，没有一条伪 ✓', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _c: string, handlers: any) => {
      for (const s of STAGES) handlers.onStage(s)
      await new Promise<void>((resolve) => {
        releaseStream = resolve
      })
      handlers.onDelta(STRUCTURED)
    })

    render(<AiCopilotPage />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    typeAndSend('600111 怎么样')

    await waitFor(() => expect(screen.getByTestId('evidence-readiness')).toBeTruthy())
    const pills = screen.getAllByTestId('evidence-readiness-item')
    expect(pills.map((p) => p.getAttribute('data-key'))).toEqual([
      'get_stock_quote',
      'get_chip_accumulation',
      'get_delta_series',
    ])
    // 关键回归：后端只发开跑帧 → 谁也不能算「已取」
    expect(pills.map((p) => p.getAttribute('data-status'))).toEqual(['pending', 'pending', 'pending'])
    for (const p of pills) expect(p.textContent).toContain('⏳')
    const strip = screen.getByTestId('evidence-readiness')
    expect(strip.textContent).toContain('已取 0/3')
    expect(strip.textContent).not.toContain('✓')
    expect(screen.queryByTestId('evidence-readiness-failed-count')).toBeNull()
    // stage 原文里能认出工具时，不再并排一条单调的「正在取数」文案
    expect(screen.queryByText(/正在取数 3\/3/)).toBeNull()

    // 放行正文 → 回答落屏，条子固化：pending 落「未确认」，不是 ✓，已取仍是 0
    await act(async () => {
      releaseStream?.()
    })
    await waitFor(() => expect(screen.getByTestId('copilot-answer-card')).toBeTruthy())
    const settled = screen.getAllByTestId('evidence-readiness-item')
    expect(settled).toHaveLength(3)
    expect(settled.map((p) => p.getAttribute('data-status'))).toEqual(['unknown', 'unknown', 'unknown'])
    for (const p of settled) expect(p.textContent).toContain('未确认')
    const settledStrip = screen.getByTestId('evidence-readiness')
    expect(settledStrip.textContent).toContain('已取 0/3')
    expect(settledStrip.textContent).not.toContain('✓')
    expect(settledStrip.textContent).not.toContain('已取 3/3')
  })

  it('失败文案帧落 failed（未取到），不会被后续开跑或流结束洗白', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _c: string, handlers: any) => {
      handlers.onStage('正在取数 1/2：get_stock_quote')
      // 防御路径：今天的后端不在 stage 里带失败文案（见文件头契约），
      // 但字符串帧一旦带「失败/超时」就必须落 failed，而不是等流结束变成功。
      handlers.onStage('正在取数 2/2：get_delta_series 取数失败')
      await new Promise<void>((resolve) => {
        releaseStream = resolve
      })
      handlers.onDelta(STRUCTURED)
    })

    render(<AiCopilotPage />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    typeAndSend('600111 怎么样')

    await waitFor(() => expect(screen.getByTestId('evidence-readiness')).toBeTruthy())
    await act(async () => {
      releaseStream?.()
    })
    await waitFor(() => expect(screen.getByTestId('copilot-answer-card')).toBeTruthy())

    const pills = screen.getAllByTestId('evidence-readiness-item')
    const byKey = Object.fromEntries(pills.map((p) => [p.getAttribute('data-key'), p]))
    expect(byKey.get_delta_series.getAttribute('data-status')).toBe('failed')
    expect(byKey.get_delta_series.textContent).toContain('未取到')
    // 没拿到结果证据的那条：未确认，不是 ✓
    expect(byKey.get_stock_quote.getAttribute('data-status')).toBe('unknown')
    expect(screen.getByTestId('evidence-readiness-failed-count').textContent).toContain('未取到 1')
    expect(screen.getByTestId('evidence-readiness').textContent).toContain('已取 0/2')
  })

  it('只有显式完成证据才算已取：混合证据下 ✓ 只给有证据的那条', () => {
    // 显式完成证据（`ok:true` / `status:'ok'`）是对象帧，今天的 SSE 不发（chat.py:2260 包成字符串）；
    // 这里用真实 reducer + 真实组件验证「万一有了证据」时 UI 不会把其余条也算成功。
    const state = settleReadiness(
      foldStageEvents([
        '正在取数 1/3：get_stock_quote',
        '正在取数 2/3：get_chip_accumulation',
        '正在取数 3/3：get_delta_series',
        { tool: 'get_chip_accumulation', status: 'ok' },
      ]),
    )
    expect(state.done).toBe(1)
    expect(state.unknown).toBe(2)

    render(<EvidenceReadinessStrip items={state.items} done={state.done} total={state.total} failed={state.failed} />)
    const pills = screen.getAllByTestId('evidence-readiness-item')
    expect(pills.map((p) => p.getAttribute('data-status'))).toEqual(['unknown', 'ok', 'unknown'])
    expect(pills[1].textContent).toContain('✓')
    expect(pills[0].textContent).toContain('未确认')
    expect(pills[2].textContent).toContain('未确认')
    expect(screen.getByTestId('evidence-readiness').textContent).toContain('已取 1/3')
  })

  it('认不出的 stage（散文提示）仍走 spinner，不污染齐套度', async () => {
    apiMock.sendMessageStream.mockImplementation(async (_id: number, _c: string, handlers: any) => {
      handlers.onStage('正在准备上下文…')
    })

    render(<AiCopilotPage />)
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    typeAndSend('看看')

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
    await waitFor(() => expect(screen.getByLabelText('发送')).toBeTruthy())
    typeAndSend('600111 怎么样')

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
