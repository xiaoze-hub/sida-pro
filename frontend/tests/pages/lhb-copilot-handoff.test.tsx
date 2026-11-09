// @vitest-environment jsdom
/**
 * 龙虎榜 → 数智参谋 交接(先红后绿, 2026-11-09)。
 *
 * 龙虎榜页面上的「分析此股龙虎榜」不能只是"跳到 /ai-copilot" —— 必须证明参谋那边
 * **真的收到了**我们声称带过去的东西, 否则就是臆造上下文:
 *  ① 入口只认 `symbol`(锚点) 与 `q`(预填问题): 参谋页必须挂出锚点条, 输入框必须已经有问题;
 *  ② 日期只在问题正文里(入口对 `?date=` 无副作用), 所以页面上能看到它 —— 用户可核对;
 *  ③ 不硬塞 `agent`: 现有配方都没有龙虎榜工具, 带上就是让参谋去答一道它没数据的题,
 *     表现是"@某个配方"的 chip 凭空出现。这里断言 chip 不存在。
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn(),
  createConversation: vi.fn().mockResolvedValue({ id: 11 }),
  fetchAPI: vi.fn(),
}))

vi.mock('@sida/api', () => ({ chatApi: apiMock, fetchAPI: apiMock.fetchAPI }))
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
import { buildLhbCopilotUrl, lhbCopilotHandoff } from '@/lib/lhb'

const URL_FOR_600000 = buildLhbCopilotUrl({ symbol: '600000', name: '浦发银行', date: '20261106' })
const URL_FOR_ETF = lhbCopilotHandoff({ symbol: '510300', name: '沪深300ETF', date: '20261106' }).url

beforeEach(() => {
  vi.clearAllMocks()
  // jsdom 没有 scrollTo(自动滚到最新消息会用到)
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function renderCopilotAt(url: string) {
  // 参谋页用 `window.location.search` 读入口参数(不读 Router state), 所以必须走真实地址,
  // 不能只给 MemoryRouter 的 initialEntries —— 否则测的是一条"入口根本不认"的路径。
  window.history.pushState({}, '', url)
  render(
    <MemoryRouter initialEntries={[url]}>
      <AiCopilot />
    </MemoryRouter>,
  )
  await waitFor(() => expect(screen.getByTestId('ai-copilot-page')).toBeTruthy())
}

describe('龙虎榜 → 参谋: 带过去的东西必须真的落地', () => {
  it('锚点股票与预填问题都已生效, 日期写在问题正文里', async () => {
    await renderCopilotAt(URL_FOR_600000)

    const anchor = screen.getByTestId('copilot-context-anchor')
    expect(anchor.textContent).toContain('600000')

    const composer = screen.getByTestId('copilot-composer')
    const textarea = within(composer).getByRole('textbox') as HTMLTextAreaElement
    expect(textarea.value).toContain('2026-11-06')
    expect(textarea.value).toContain('600000')
    expect(textarea.value).toContain('龙虎榜')
    // 预填但不自动发送: 空会话, 没有消息气泡
    expect(screen.getByTestId('copilot-empty-hero')).toBeTruthy()
  })

  it('没有 agent 参数就不会凭空多出一个配方 chip', async () => {
    await renderCopilotAt(URL_FOR_600000)
    expect(screen.queryByTestId('copilot-mention-chips')).toBeNull()
  })

  it('ETF 代码不给锚点: 参谋页没有锚点条, 只预填问题(未发送)', async () => {
    await renderCopilotAt(URL_FOR_ETF)

    // 入口 SYMBOL_RE 只认 [03468] 开头的 6 位 A 股代码; 510300 不是股票 → 不许有锚点条
    expect(screen.queryByTestId('copilot-context-anchor')).toBeNull()

    const composer = screen.getByTestId('copilot-composer')
    const textarea = within(composer).getByRole('textbox') as HTMLTextAreaElement
    expect(textarea.value).toContain('510300')
    expect(textarea.value).toContain('2026-11-06')
    // 只预填, 不自动发送
    expect(screen.getByTestId('copilot-empty-hero')).toBeTruthy()
  })
})
