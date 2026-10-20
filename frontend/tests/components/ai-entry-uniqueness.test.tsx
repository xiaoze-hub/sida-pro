// @vitest-environment jsdom
/**
 * AI 入口唯一性（2026-10-18）：
 *  - 个股工作台胶囊在场时，ChatWidget 的圆形 FAB 必须隐藏（只留胶囊）；
 *  - 离开工作台（胶囊注销）后，FAB 必须回来 —— 其它页面它仍是唯一入口。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@panwatch/api', () => ({
  chatApi: {
    listConversations: vi.fn().mockResolvedValue([]),
    getConversation: vi.fn().mockResolvedValue({ messages: [] }),
    getSuggestedQuestions: vi.fn().mockResolvedValue({ questions: [] }),
    createConversation: vi.fn().mockResolvedValue({ id: 1 }),
  },
  fetchAPI: vi.fn().mockResolvedValue({ items: [] }),
}))

vi.mock('@panwatch/base-ui/components/ui/toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}))

const briefMock = vi.hoisted(() => ({ getCopilotBrief: vi.fn() }))
vi.mock('../../src/lib/copilot/api', () => ({
  getPendingActions: vi.fn(async () => []),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),

  getCopilotBrief: (...a: unknown[]) => briefMock.getCopilotBrief(...a),
  getCopilotPresets: vi.fn().mockResolvedValue([]),
}))

import ChatWidget from '../../src/components/ChatWidget'
import { CopilotCapsule } from '../../src/components/copilot/CopilotCapsule'
import { resetCapsulePresenceForTest } from '../../src/lib/copilot/capsule-presence'

const FAB_TITLE = '打开数智分析BOT'

beforeEach(() => {
  localStorage.clear()
  resetCapsulePresenceForTest()
  briefMock.getCopilotBrief.mockResolvedValue({
    symbol: '600111',
    market: 'CN',
    name: '北方稀土',
    as_of: '2026-10-18T10:31:00+08:00',
    quote: { close: 35.32, change_pct: 1.2 },
    evidence: {
      chips: {},
      resonance: {},
      microstructure: {},
    },
    verdict: { intent_label: '低位单峰 · 筹码锁定良好', intent_tone: 'opportunity', confidence: 0.72 },
  })
})
afterEach(cleanup)

describe('AI 入口唯一性', () => {
  it('没有胶囊时（其它页面）保留 ChatWidget FAB', () => {
    render(<ChatWidget />)
    expect(screen.getByTitle(FAB_TITLE)).toBeTruthy()
  })

  it('数智参谋一级页内不再渲染 FAB（页面本身就是入口）', () => {
    window.history.pushState({}, '', '/ai-copilot')
    try {
      render(<ChatWidget />)
      expect(screen.queryByTitle(FAB_TITLE)).toBeNull()
    } finally {
      window.history.pushState({}, '', '/')
    }
  })

  it('胶囊在场时隐藏 FAB，只留胶囊；胶囊卸载后 FAB 恢复', async () => {
    const first = render(
      <>
        <ChatWidget />
        <CopilotCapsule symbol="600111" />
      </>,
    )
    await screen.findByTestId('copilot-capsule')
    await waitFor(() => expect(screen.queryByTitle(FAB_TITLE)).toBeNull())
    expect(screen.getByTestId('copilot-capsule-open')).toBeTruthy()

    first.unmount()
    render(<ChatWidget />)
    await waitFor(() => expect(screen.getByTitle(FAB_TITLE)).toBeTruthy())
  })
})
