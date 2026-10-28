// @vitest-environment jsdom
/**
 * 空头司令挑刺（红蓝对抗压力测试）面板（2026-10-24 创新 6）守七件事：
 *
 * ① **醒目警示红按钮**：文案「呼叫空头司令挑刺（压力测试）」，用 --destructive 语义色板
 *    （危险操作警示，与买红卖绿的行情色板无关），面板头部 hairline 分隔（无卡片底）；
 * ② **挂载不触发**：不点按钮不发任何请求（不建会话、不发消息）；
 * ③ **点击走现有参谋触发路径**：先 createConversation({stock_symbol, stock_market})，
 *    再 sendMessageStream(conv.id, 含配方关键词与 6 位代码的问句, …, {symbol, market}) ——
 *    后端据此确定性路由到 devil_advocate 配方并解析标的；busy 态禁用按钮；
 * ④ **流式结果真渲染**：onDelta 累积、onDone 用落库全文，正文交给 SafeMarkdown（GFM 表格），
 *    组件不二次加工 AI 输出；
 * ⑤ **失败如实陈述**：onError 展示后端 error 原文，按钮恢复可重试，不谎称已生成；
 * ⑥ **没有终稿就是没完成**：流静默结束（既无 done 也无 error）/ result.failure 有值 →
 *    明确判失败并标「不完整」（已流出的片段保留可见），不再「有增量就判 done」；
 *    终稿（authoritative）到达才按落库全文显示。
 * ⑦ **集成锚点**：SuggestTab（工作台「建议」页）必须真的挂载本面板。
 *
 * 真数据纪律：mock 的是网络层（@sida/api 的 chatApi/insightApi 等）与 SafeMarkdown（探针），
 * 组件逻辑全走真实代码。不触发真实对话/交易，也没有任何下单路径（只出报告）。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@sida/base-ui/components/ui/toast'

/** sendMessageStream 的第 3 个参数（本组件只用这四个回调）。 */
interface StreamHandlers {
  onStage?: (message: string) => void
  onDelta?: (content: string) => void
  onDone?: (message: { content: string }) => void
  onError?: (message: string) => void
}

/** 把「只关心 handlers 的测试实现」包成 mock 可接受的 rest 参数实现（宽进，不丢类型）。 */
const streamImpl = (fn: (h: StreamHandlers) => Promise<void>) =>
  async (...args: unknown[]) => { await fn((args[2] || {}) as StreamHandlers) }

const mocks = vi.hoisted(() => ({
  createConversation: vi.fn<(...a: unknown[]) => Promise<{ id: number; title: string }>>(),
  /** 返回 `ChatStreamResult`（结论真源）；本文件多数用例只关心 handlers，故按 unknown 宽进。 */
  sendMessageStream: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  fetchAPI: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightQuote: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightMoreInfo: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightDarkFlowTq: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightKlineSummary: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightKlines: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightPortfolioSummary: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightSuggestions: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightNews: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightHistory: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  insightCompany: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  stocksList: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  stocksCreate: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  stocksRemove: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  stocksUpdateAgents: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  stocksTriggerAgent: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  tradingLatest: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  tradingComparison: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
  fundamentalsDetail: vi.fn<(...a: unknown[]) => Promise<unknown>>(),
}))

vi.mock('@sida/api', () => ({
  chatApi: {
    createConversation: (...a: unknown[]) => mocks.createConversation(...a),
    sendMessageStream: (...a: unknown[]) => mocks.sendMessageStream(...a),
  },
  insightApi: {
    quote: (...a: unknown[]) => mocks.insightQuote(...a),
    moreInfo: (...a: unknown[]) => mocks.insightMoreInfo(...a),
    darkFlowTq: (...a: unknown[]) => mocks.insightDarkFlowTq(...a),
    klineSummary: (...a: unknown[]) => mocks.insightKlineSummary(...a),
    klines: (...a: unknown[]) => mocks.insightKlines(...a),
    portfolioSummary: (...a: unknown[]) => mocks.insightPortfolioSummary(...a),
    suggestions: (...a: unknown[]) => mocks.insightSuggestions(...a),
    news: (...a: unknown[]) => mocks.insightNews(...a),
    history: (...a: unknown[]) => mocks.insightHistory(...a),
    company: (...a: unknown[]) => mocks.insightCompany(...a),
  },
  stocksApi: {
    list: (...a: unknown[]) => mocks.stocksList(...a),
    create: (...a: unknown[]) => mocks.stocksCreate(...a),
    remove: (...a: unknown[]) => mocks.stocksRemove(...a),
    updateAgents: (...a: unknown[]) => mocks.stocksUpdateAgents(...a),
    triggerAgent: (...a: unknown[]) => mocks.stocksTriggerAgent(...a),
  },
  tradingAgentsApi: {
    getLatestForStock: (...a: unknown[]) => mocks.tradingLatest(...a),
    getHistoryComparison: (...a: unknown[]) => mocks.tradingComparison(...a),
  },
  fundamentalsApi: { detail: (...a: unknown[]) => mocks.fundamentalsDetail(...a) },
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
}))

// SafeMarkdown 换成探针：断言「AI 正文原样进渲染器、不被二次加工」
vi.mock('@/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md">{children}</div>,
}))

import { DevilAdvocatePanel } from '@/components/copilot/DevilAdvocatePanel'
import SuggestTab from '@/pages/workbench/tabs/SuggestTab'

/** 参谋配方产出的一段标准正文（结论前置 + GFM 表格 + 要点行）。 */
const RECIPE_ANSWER = [
  '【一句话结论】上方套牢峰压制 + 逐笔主力净流出，反弹抛压最致命。',
  '',
  '【压力情景】',
  '| 维度 | 数值 | 口径 | 判读 |',
  '| --- | --- | --- | --- |',
  '| 现价 | 11.80 | TQ | — |',
  '| 亏15%对应价位 | 10.03（推算） | — | 关键位下方 |',
  '',
  '【死因一】',
  '- 逐笔主力净额 -386 万，派发意图（tick 口径）',
].join('\n')

beforeEach(() => {
  mocks.createConversation.mockResolvedValue({ id: 77, title: '' })
  mocks.sendMessageStream.mockImplementation(
    streamImpl(async (h) => {
      h.onStage?.('正在取数 1/10：get_main_intent')
      h.onDelta?.('【一句话结论】上方套牢峰压制')
      h.onDelta?.(' + 逐笔主力净流出，反弹抛压最致命。')
      h.onDone?.({ content: RECIPE_ANSWER })
    }),
  )
  mocks.insightQuote.mockResolvedValue({ symbol: '002636', market: 'CN', name: '测试标的', current_price: 11.8 })
  mocks.insightMoreInfo.mockResolvedValue({})
  mocks.insightDarkFlowTq.mockResolvedValue({})
  mocks.insightKlineSummary.mockResolvedValue({ summary: null })
  mocks.insightKlines.mockResolvedValue({ klines: [] })
  mocks.insightPortfolioSummary.mockResolvedValue({ accounts: [] })
  mocks.insightSuggestions.mockResolvedValue([])
  mocks.insightNews.mockResolvedValue([])
  mocks.insightHistory.mockResolvedValue([])
  mocks.insightCompany.mockResolvedValue({})
  mocks.stocksList.mockResolvedValue([])
  mocks.stocksTriggerAgent.mockResolvedValue({})
  mocks.tradingLatest.mockResolvedValue(null)
  mocks.tradingComparison.mockResolvedValue(null)
  mocks.fundamentalsDetail.mockResolvedValue({})
  mocks.fetchAPI.mockResolvedValue({}) // ThreeTierVerdictRow 等直连 fetchAPI 的组件
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const triggerButton = () => screen.getByTestId('devil-advocate-trigger') as HTMLButtonElement

function renderPanel() {
  return render(<DevilAdvocatePanel symbol="002636" market="CN" />)
}

describe('空头司令挑刺（DevilAdvocatePanel）', () => {
  it('警示红按钮文案与语义色板正确；hairline 分隔无卡片底；挂载不触发任何请求', () => {
    renderPanel()

    const btn = triggerButton()
    expect(btn.textContent).toContain('呼叫空头司令挑刺（压力测试）')
    // 警示红走 --destructive 语义令牌（danger 警示 ≠ 买红卖绿行情色板）
    expect(btn.className).toContain('text-destructive')
    expect(btn.className).toContain('bg-destructive/10')
    expect(btn.className).not.toMatch(/shadow-float/)
    // hairline：面板容器用 border-t 分隔，不是整圈卡片边框
    const panel = screen.getByTestId('devil-advocate-panel')
    expect(panel.className).toContain('border-t')
    expect(panel.className).not.toMatch(/rounded/)
    // 挂载不触发：不建会话、不发消息
    expect(mocks.createConversation).not.toHaveBeenCalled()
    expect(mocks.sendMessageStream).not.toHaveBeenCalled()
    expect(screen.queryByTestId('devil-advocate-result')).toBeNull()
  })

  it('点击走现有参谋路径：建会话(带标的) → 流式消息(含配方关键词+代码)；busy 态禁用按钮', async () => {
    let release: (() => void) | null = null
    mocks.sendMessageStream.mockImplementation(
      () => new Promise<void>((resolve) => { release = () => resolve() }),
    )
    renderPanel()

    fireEvent.click(triggerButton())

    await waitFor(() => expect(mocks.createConversation).toHaveBeenCalledTimes(1))
    expect(mocks.createConversation).toHaveBeenCalledWith({ stock_symbol: '002636', stock_market: 'CN' })

    await waitFor(() => expect(mocks.sendMessageStream).toHaveBeenCalledTimes(1))
    const firstCall = mocks.sendMessageStream.mock.calls[0] ?? []
    const [convId, content, , , , stock] = firstCall
    expect(convId).toBe(77)
    // 问句必须含配方关键词（确定性路由到 devil_advocate）与 6 位代码（管线解析标的）
    expect(String(content)).toContain('空头司令')
    expect(String(content)).toContain('压力测试')
    expect(String(content)).toContain('002636')
    expect(stock).toEqual({ symbol: '002636', market: 'CN' })

    // busy 态：按钮禁用 + 文案变化；stage 提示可见
    expect(triggerButton().disabled).toBe(true)
    expect(screen.getByTestId('devil-advocate-trigger').textContent).toContain('研判中')
    await waitFor(() => expect(screen.getByTestId('devil-advocate-stage')).toBeTruthy())

    await act(async () => { release?.() })
    await waitFor(() => expect(triggerButton().disabled).toBe(false))
    expect(screen.queryByTestId('devil-advocate-stage')).toBeNull()
  })

  it('流式结果真渲染：onDone 落库全文进 SafeMarkdown（含 GFM 表格与口径行），组件不二次加工', async () => {
    renderPanel()
    fireEvent.click(triggerButton())

    await waitFor(() => expect(screen.getByTestId('devil-advocate-result')).toBeTruthy())
    const md = screen.getByTestId('md')
    expect(md.textContent).toBe(RECIPE_ANSWER)
    // 免责行必须在场（逆向情报非指令 + 无数据/无数据源图例）
    expect(screen.getByTestId('devil-advocate-result').textContent).toContain('非买卖指令')
    expect(screen.getByTestId('devil-advocate-result').textContent).toContain('无数据源')
    expect(screen.queryByTestId('devil-advocate-failed')).toBeNull()
  })

  it('onError：展示后端 error 原文 + 如实陈述未完成与未下单，按钮恢复可重试', async () => {
    mocks.sendMessageStream.mockImplementation(
      streamImpl(async (h) => { h.onError?.('AI 服务暂时不可用：quota exceeded') }),
    )
    renderPanel()
    fireEvent.click(triggerButton())

    const failed = await screen.findByTestId('devil-advocate-failed')
    expect(failed.textContent).toContain('挑刺失败')
    expect(failed.textContent).toContain('AI 服务暂时不可用：quota exceeded')
    expect(failed.textContent).toContain('本次未发生任何交易委托')
    expect(screen.queryByTestId('devil-advocate-result')).toBeNull()
    expect(triggerButton().disabled).toBe(false)
  })

  it('流静默结束且空流：如实失败、不装成功', async () => {
    // 空流（什么帧都没下发，result 也没有终稿）→ 如实失败
    mocks.sendMessageStream.mockImplementation(async () => {})
    renderPanel()
    fireEvent.click(triggerButton())
    await waitFor(() => expect(screen.getByTestId('devil-advocate-failed')).toBeTruthy())
    expect(screen.getByTestId('devil-advocate-failed').textContent).toContain('未生成报告')
    expect(screen.queryByTestId('devil-advocate-result')).toBeNull()
    expect(screen.queryByTestId('devil-advocate-incomplete')).toBeNull()
  })
})

/**
 * 2026-10-28 定向回归：本面板是 `sendMessageStream` 的另一个调用方。
 * 旧实现「流静默结束但有增量 → 判 done」把断流的半截正文当成品亮着；
 * 参谋页那边（AiCopilot）早已改成「没终稿就是失败」，这里必须同一条规矩。
 */
describe('流结论以 stream result 为准（与参谋页同一契约）', () => {
  const PARTIAL = '【一句话结论】上方套牢峰压制（写到一半就断了'

  it('无 done、有增量、result.failure → 明确判失败：失败态原文可见 + 片段保留但标不完整，按钮可重试', async () => {
    mocks.sendMessageStream.mockImplementation(async (...args: unknown[]) => {
      const h = (args[2] || {}) as StreamHandlers
      h.onStage?.('正在取数 1/10：get_main_intent')
      h.onDelta?.(PARTIAL)
      return {
        content: PARTIAL,
        authoritative: false,
        corruptedFrames: 1,
        errors: [],
        failure: '流式数据损坏（1 处），且未收到服务端终稿，回答可能不完整',
      }
    })

    renderPanel()
    fireEvent.click(triggerButton())

    const failed = await screen.findByTestId('devil-advocate-failed')
    // 不再谎称"完成"：文案是"未完成/中途断流"，并照抄 failure 原文
    expect(failed.textContent).toContain('中途断流')
    expect(failed.textContent).toContain('流式数据损坏（1 处）')
    expect(failed.textContent).toContain('本次未发生任何交易委托')

    // 片段留在屏上（有信息量），但必须带不完整标记
    expect(screen.getByTestId('devil-advocate-incomplete').textContent).toContain('不完整')
    expect(screen.getByTestId('md').textContent).toBe(PARTIAL)

    // 可重试 + 不在 running 态
    expect(triggerButton().disabled).toBe(false)
    expect(screen.queryByTestId('devil-advocate-stage')).toBeNull()
  })

  it('无 done、有增量、但 result 契约缺省（老实现只 resolve 空值）→ 同样不许判 done', async () => {
    mocks.sendMessageStream.mockImplementation(streamImpl(async (h) => { h.onDelta?.(PARTIAL) }))

    renderPanel()
    fireEvent.click(triggerButton())

    await screen.findByTestId('devil-advocate-failed')
    expect(screen.getByTestId('devil-advocate-incomplete')).toBeTruthy()
    expect(screen.getByTestId('md').textContent).toBe(PARTIAL)
    expect(triggerButton().disabled).toBe(false)
  })

  it('终稿到达（authoritative）：按落库全文显示，不标不完整、不误报失败（坏帧也不翻案）', async () => {
    mocks.sendMessageStream.mockImplementation(async (...args: unknown[]) => {
      const h = (args[2] || {}) as StreamHandlers
      h.onDelta?.(PARTIAL)
      h.onDone?.({ content: RECIPE_ANSWER })
      return { content: RECIPE_ANSWER, authoritative: true, corruptedFrames: 2, errors: [] }
    })

    renderPanel()
    fireEvent.click(triggerButton())

    await waitFor(() => expect(screen.getByTestId('md').textContent).toBe(RECIPE_ANSWER))
    expect(screen.queryByTestId('devil-advocate-failed')).toBeNull()
    expect(screen.queryByTestId('devil-advocate-incomplete')).toBeNull()
    expect(screen.getByTestId('devil-advocate-result').textContent).toContain('非买卖指令')
    expect(triggerButton().disabled).toBe(false)
  })

  it('服务端 error 帧（result.failure 同源）→ 失败原文照实展示，不推断原因', async () => {
    mocks.sendMessageStream.mockImplementation(async (...args: unknown[]) => {
      const h = (args[2] || {}) as StreamHandlers
      h.onError?.('AI 服务暂时不可用：quota exceeded')
      return {
        content: '',
        authoritative: false,
        corruptedFrames: 0,
        errors: ['AI 服务暂时不可用：quota exceeded'],
        failure: 'AI 服务暂时不可用：quota exceeded',
      }
    })

    renderPanel()
    fireEvent.click(triggerButton())

    const failed = await screen.findByTestId('devil-advocate-failed')
    expect(failed.textContent).toContain('AI 服务暂时不可用：quota exceeded')
    expect(screen.queryByTestId('devil-advocate-result')).toBeNull()
    expect(screen.queryByTestId('devil-advocate-incomplete')).toBeNull()
  })
})

describe('SuggestTab 集成：空头司令入口挂载在「建议」页', () => {
  it('SuggestTab 渲染 DevilAdvocatePanel（触发按钮可见，无挂载副作用）', async () => {
    render(
      <MemoryRouter>
        <ToastProvider>
          <SuggestTab symbol="002636" market="CN" hasPosition />
        </ToastProvider>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByTestId('devil-advocate-trigger')).toBeTruthy())
    expect(screen.getByTestId('devil-advocate-trigger').textContent).toContain('呼叫空头司令挑刺（压力测试）')
    // 挂载本身不发起压力测试（按钮是手动触发）
    expect(mocks.createConversation).not.toHaveBeenCalled()
    expect(mocks.sendMessageStream).not.toHaveBeenCalled()
  })
})
