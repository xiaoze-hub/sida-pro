// @vitest-environment jsdom
/**
 * 参谋功能入口整合（2026-11-07）。
 *
 * 背景：参谋有 7 个全页视图（任务回放 / 经验库 / 工具目录 / Agent 目录 / 能力提案 /
 * 并行诊断 / 治理拦截），App.tsx 里路由都在、navigation.ts 里页签标题都在，但参谋页上
 * 只挂了一条「治理」文本链接，其余 6 个只能靠背 URL 进。
 *
 * 本文件钉住整合后的四条边界（不重复实现导航，只做入口）：
 *  ① 目的地必须都是已注册的页签路由 —— 不新造 URL、不产生死链，老地址不受影响；
 *  ② 只有一个入口，且原「治理」单链接被收进菜单（同一目的地不出现两次）；
 *  ③ 打开入口**不挂第二侧栏、不挂第二个参谋实例、不发请求** —— 本页会话/过程面板不动；
 *  ④ rail 里原有的「过程 / 记忆 / 定时 / 提案」四个内嵌面板仍在（整页入口不替代它们）。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
import { COPILOT_ENTRY_LINKS } from '@/components/copilot/CopilotEntryMenu'
import { SECONDARY_ROUTES } from '@/lib/navigation'

beforeEach(() => {
  vi.clearAllMocks()
  // jsdom 没有 scrollTo（自动滚动到最新消息会用到）
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  window.history.pushState({}, '', '/ai-copilot')
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function renderPage() {
  render(<MemoryRouter initialEntries={['/ai-copilot']}><AiCopilot /></MemoryRouter>)
  await waitFor(() => expect(screen.getByTestId('ai-copilot-page')).toBeTruthy())
}

describe('参谋全页入口：只指已注册路由', () => {
  it('每个入口都是 navigation.ts 登记的页签路由，且互不重复', () => {
    const registered = new Set(SECONDARY_ROUTES.map((route) => route.path))
    for (const link of COPILOT_ENTRY_LINKS) {
      expect(registered.has(link.href), `${link.href} 不在 SECONDARY_ROUTES 里（会变成死链或裸路径页签）`).toBe(true)
    }
    // 同一目的地只出现一次；也不把参谋页自己列进入口
    const hrefs = COPILOT_ENTRY_LINKS.map((link) => link.href)
    expect(new Set(hrefs).size).toBe(hrefs.length)
    expect(hrefs).not.toContain('/ai-copilot')
    // 四个被点名的路径（配方实验 / 运行 / 工具 / 提案）一个不少
    for (const href of ['/copilot/runs', '/copilot/experiments', '/copilot/tools', '/copilot/proposals']) {
      expect(hrefs).toContain(href)
    }
  })

  it('菜单里的链接是普通 href（同标签页整页打开），不开新窗、不劫持路由', async () => {
    await renderPage()
    fireEvent.click(screen.getByTestId('copilot-entry-menu-trigger'))
    for (const link of COPILOT_ENTRY_LINKS) {
      const anchor = screen.getByTestId(`copilot-entry-${link.key}`) as HTMLAnchorElement
      expect(anchor.getAttribute('href')).toBe(link.href)
      expect(anchor.getAttribute('target')).toBeNull()
      expect(anchor.getAttribute('rel')).toBeNull()
    }
  })
})

describe('参谋全页入口：一个下拉，可发现但不重复', () => {
  it('点击入口展开菜单，7 个目的地都能看到；Escape 收起并同步 aria', async () => {
    await renderPage()
    const trigger = screen.getByTestId('copilot-entry-menu-trigger')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByTestId('copilot-entry-menu')).toBeNull()

    fireEvent.click(trigger)
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByTestId('copilot-entry-menu')).toBeTruthy()
    expect(screen.getAllByRole('menuitem')).toHaveLength(COPILOT_ENTRY_LINKS.length)

    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('copilot-entry-menu')).toBeNull())
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })

  it('原「治理」单链接收进菜单：同一目的地不在工具条出现两次', async () => {
    await renderPage()
    // 旧入口已移除（治理只在菜单里出现一次）
    expect(screen.queryByTestId('copilot-governance-link')).toBeNull()
    fireEvent.click(screen.getByTestId('copilot-entry-menu-trigger'))
    expect(screen.getByTestId('copilot-entry-governance').getAttribute('href')).toBe('/copilot-governance')
    expect(screen.getAllByText('治理拦截')).toHaveLength(1)
  })

  it('rail 里原有的四个内嵌面板入口仍在（整页入口不替代它们）', async () => {
    await renderPage()
    expect(screen.getByTestId('copilot-conversation-rail')).toBeTruthy()
    expect(screen.getByTestId('copilot-panel-runs')).toBeTruthy()
    expect(screen.getByTestId('copilot-more-panels')).toBeTruthy()
    expect(screen.getByTestId('copilot-panel-memory')).toBeTruthy()
    expect(screen.getByTestId('copilot-panel-schedule')).toBeTruthy()
    expect(screen.getByTestId('copilot-panel-proposals')).toBeTruthy()
  })
})

describe('参谋全页入口：不改变本页实例与会话', () => {
  it('打开入口不挂第二侧栏，也不发请求', async () => {
    // 让页面挂载阶段的请求保持 pending：计数固定，才能断言「打开菜单零请求」。
    const fetchSpy = vi.fn(() => new Promise<Response>(() => {}))
    vi.stubGlobal('fetch', fetchSpy)
    await renderPage()
    const before = fetchSpy.mock.calls.length

    const trigger = screen.getByTestId('copilot-entry-menu-trigger')
    act(() => {
      fireEvent.click(trigger)
    })
    expect(screen.getByTestId('copilot-entry-menu')).toBeTruthy()
    expect(fetchSpy.mock.calls.length).toBe(before)

    // 只有一个参谋页、一条会话栏；菜单不是侧栏（不产生 copilot-side-panel）
    expect(document.querySelectorAll('[data-testid="ai-copilot-page"]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-testid="copilot-conversation-rail"]')).toHaveLength(1)
    expect(screen.queryByTestId('copilot-side-panel')).toBeNull()
  })

  it('打开 rail 过程面板后仍只有一个页实例与一条会话栏', async () => {
    await renderPage()
    fireEvent.click(screen.getByTestId('copilot-panel-runs'))
    await waitFor(() => expect(screen.getByTestId('copilot-side-panel')).toBeTruthy())
    expect(document.querySelectorAll('[data-testid="ai-copilot-page"]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-testid="copilot-conversation-rail"]')).toHaveLength(1)
    // 侧栏打开时入口仍在工具条上，不因面板折叠/展开而消失
    expect(screen.getByTestId('copilot-entry-menu-trigger')).toBeTruthy()
  })
})
