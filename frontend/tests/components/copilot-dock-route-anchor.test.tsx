// @vitest-environment jsdom
/**
 * 停靠面板（mini）的**页面锚点跟随**（2026-11-18）。
 *
 * 现场问题（真浏览器抓包）：个股页上点「问参谋」打开的停靠面板，请求体只有 `{"content"}` ——
 * `AiCopilot` 只有在 `sida-open-chat`（胶囊 entry）那一刻才会 `setAnchorSymbol`，
 * 用户直接点悬浮球打开、或换股之后，锚点就一直空着：参谋不知道你在看哪只票，
 * 图表快照也没有可归属的标的。
 *
 * 钉住的口径：
 *   ① mini/停靠模式跟随当前路由 `/stocks/:symbol`（`?type=index` = 指数），
 *      请求体带上 `stock_symbol` / `stock_market` / `stock_instrument`；
 *   ② 换股跟新，旧图的快照一个字符都不许带；
 *   ③ 离开个股页 → 锚点清空（不把上一只票当成"还在看"）；
 *   ④ 用户**自己写明**的标的（@提及 / 正文代码 / entry 带入的另一只票）优先，
 *      路由不许覆盖它；
 *   ⑤ 图上没有那一份快照就不带（不许拿别的票/上一条记录凑数）。
 *
 * 这里用**真 AiCopilot + 真 StockWorkbench + 真 chatApi.sendMessageStream**
 * （只把全局 fetch 换成一个会记账的 SSE 后端），断言的是**真正发出去的请求体**，
 * 不是"mock 函数被调用过"。
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'

const DIGEST_A = 'a'.repeat(64)
const DIGEST_B = 'b'.repeat(64)
const OWNER = 'test-user'

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  portfolioSummary: vi.fn(),
}))

vi.mock('@sida/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@sida/api')>()
  return {
    ...actual,
    fetchAPI: mocks.fetchAPI,
    dashboardApi: { ...actual.dashboardApi, portfolioSummary: mocks.portfolioSummary },
  }
})

/** 图表替身：按当前 props 的标的/类型上报一份快照，卸载时按真组件契约回调 null。 */
vi.mock('@sida/biz-ui/components/KlineChart', async () => {
  const { useEffect, useRef } = await import('react')
  function KlineStub(p: {
    symbol: string
    market: string
    instrument?: 'stock' | 'index'
    onChanSnapshotChange?: (s: unknown) => void
    onIntervalChange?: (i: string) => void
  }) {
    const cb = useRef(p.onChanSnapshotChange)
    cb.current = p.onChanSnapshotChange
    useEffect(() => () => cb.current?.(null), [])
    const digest = p.symbol === '600111' ? 'a'.repeat(64) : p.symbol === '000776' ? 'b'.repeat(64) : 'c'.repeat(64)
    return (
      <div data-testid="kline" data-symbol={`${p.symbol}:${p.market}:${p.instrument ?? ''}`}>
        <button
          type="button"
          onClick={() =>
            p.onChanSnapshotChange?.({
              symbol: p.symbol,
              market: p.market,
              instrument: p.instrument ?? 'stock',
              interval: '1d',
              days: 250,
              digest,
            })
          }
        >
          chart-emit
        </button>
        <button type="button" onClick={() => p.onChanSnapshotChange?.(null)}>
          chart-emit-null
        </button>
      </div>
    )
  }
  return { default: KlineStub }
})

vi.mock('@/hooks/useWorkbenchAnnotations', () => ({
  useWorkbenchAnnotations: () => ({ snapshot: { key: '', phase: 'ready', lines: [], markers: [] }, priceLines: undefined }),
}))
vi.mock('@/hooks/useCopilotBrief', () => ({ useCopilotBrief: () => ({ data: null }) }))
vi.mock('@/hooks/useSourceHealth', () => ({
  useSourceHealth: () => ({ health: {}, loading: false, isReady: () => true, reasonOf: () => '' }),
}))
vi.mock('@sida/biz-ui/components/workbench/HeaderBand', () => ({
  default: (p: { symbol: string }) => <div data-testid="band1">{p.symbol}</div>,
}))
vi.mock('@sida/biz-ui/components/workbench/QuickRail', () => ({ default: () => <div data-testid="rail" /> }))
vi.mock('@/pages/workbench/IndexBody', () => ({ default: () => <div data-testid="index-body" /> }))
vi.mock('@sida/biz-ui/components/workbench/BoardBody', () => ({ default: () => <div data-testid="board-body" /> }))
vi.mock('@/components/DisciplineCoolDownBanner', () => ({ DisciplineCoolDownBanner: () => null }))
vi.mock('@/components/DefenseLineBanner', () => ({ DefenseLineBanner: () => null }))
vi.mock('@sida/biz-ui/components/Fund4Strip', () => ({ default: () => null }))
vi.mock('@/components/RangeStatsCard', () => ({ default: () => <div data-testid="range-stats" /> }))
vi.mock('@/components/copilot/AnnotationLinkBar', () => ({ AnnotationLinkBar: () => null }))

function tabMock(id: string) {
  return async () => ({ default: () => <div data-testid={`tab-${id}`} /> })
}
vi.mock('@/pages/workbench/tabs/L2Tab', tabMock('l2'))
vi.mock('@/pages/workbench/tabs/SuggestTab', tabMock('suggest'))
vi.mock('@/pages/workbench/tabs/FundamentalTab', tabMock('fundamental'))
vi.mock('@/pages/workbench/tabs/NewsTab', tabMock('news'))
vi.mock('@/pages/workbench/tabs/ResearchTab', tabMock('research'))
vi.mock('@/pages/workbench/tabs/ForecastTab', tabMock('forecast'))

vi.mock('@/lib/copilot/api', () => ({
  getCopilotMetrics: vi.fn(async () => ({ days: 7, runs: 0, ok_rate: null, avg_tools: null, p95_elapsed_ms: null, by_recipe: [] })),
  getPendingActions: vi.fn(async () => []),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),
  getCopilotPresets: vi.fn(async () => []),
  getCopilotBrief: vi.fn(async () => null),
  getCopilotMentions: vi.fn(async () => ({ stocks: [], agents: [] })),
}))
vi.mock('@/hooks/useConversations', () => ({
  useConversations: () => ({
    conversations: [],
    list: vi.fn(async () => {}),
    rename: vi.fn(async () => ({ ok: true })),
    pin: vi.fn(async () => ({ ok: true })),
    remove: vi.fn(async () => ({ ok: true })),
  }),
}))
vi.mock('@/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md-fallback">{children}</div>,
}))
vi.mock('@/components/copilot/CopilotBriefCard', () => ({ CopilotBriefCard: () => <div data-testid="brief-card" /> }))
vi.mock('@/components/copilot/LiveRunPanel', () => ({ LiveRunPanel: () => <div data-testid="live-run-panel" /> }))

import StockWorkbench from '@/pages/StockWorkbench'
import AiCopilotPage from '@/pages/AiCopilot'
import type { AssistantEntry } from '@/components/AssistantWorkspace'
import { readStockRouteAnchor } from '@/lib/copilot/route-anchor'
import { clearChanSnapshotStore, clearChartContextStore } from '@/lib/copilot/chart-context'

/**
 * 真 `chatApi` 走的是**全局 fetch**（`fetchAPI` 是模块内部引用，替不掉），
 * 所以这里用一个会记账的「假后端」同时承担：普通接口的信封 JSON + 流式 SSE。
 */
const sentBodies: Array<Record<string, unknown>> = []
const convPayloads: Array<Record<string, unknown>> = []

function envelope(data: unknown): Response {
  return new Response(JSON.stringify({ code: 0, success: true, data, message: '' }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function sseResponse(): Response {
  const frames =
    'event: done\ndata: ' +
    JSON.stringify({ id: 7, role: 'assistant', content: '收到。', run_id: 'run-1', created_at: '' }) +
    '\n\n'
  return new Response(frames, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

function installBackend() {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String((input as Request).url ?? input)
    const method = (init?.method ?? 'GET').toUpperCase()
    if (url.includes('/messages/stream')) {
      sentBodies.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>)
      return sseResponse()
    }
    if (url.includes('/chat/conversations') && method === 'POST') {
      convPayloads.push(JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>)
      return envelope({ id: 21, title: 't', created_at: '' })
    }
    return envelope([])
  }))
}

/** 页面按钮：真路由跳转（停靠面板不卸载 —— 这正是生产里的拓扑）。 */
function Probe() {
  const navigate = useNavigate()
  return (
    <div>
      <button type="button" onClick={() => navigate('/stocks/000776?period=d1')}>go-000776</button>
      <button type="button" onClick={() => navigate('/stocks/601318?period=d1')}>go-601318</button>
      <button type="button" onClick={() => navigate('/stocks/000001?type=index')}>go-index</button>
      <button type="button" onClick={() => navigate('/dashboard')}>go-dashboard</button>
    </div>
  )
}

/** 生产接线：工作区把「当前路由那个标的」交给停靠面板（这里用同一个纯函数复现）。 */
function DockedCopilot({ entry }: { entry?: AssistantEntry | null }) {
  const location = useLocation()
  return (
    <AiCopilotPage
      presentation="mini"
      entry={entry ?? null}
      routeAnchor={readStockRouteAnchor(location.pathname, location.search)}
    />
  )
}

function renderDock(entry?: AssistantEntry) {
  return render(
    <MemoryRouter initialEntries={['/stocks/600111?period=d1']}>
      <Probe />
      <Routes>
        <Route path="/stocks/:symbol" element={<StockWorkbench />} />
        <Route path="/dashboard" element={<div data-testid="dashboard" />} />
      </Routes>
      <DockedCopilot entry={entry} />
    </MemoryRouter>,
  )
}

function composer() {
  return within(screen.getByTestId('copilot-composer'))
}

async function ask(question: string) {
  await waitFor(() => expect(composer().getByRole('textbox')).toBeTruthy())
  const box = composer().getByRole('textbox')
  fireEvent.change(box, { target: { value: question } })
  fireEvent.click(composer().getByLabelText('发送'))
  await waitFor(() => expect(sentBodies.length).toBeGreaterThan(0))
}

function lastBody() {
  return sentBodies[sentBodies.length - 1]
}

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: OWNER }))}.signature`)
  window.history.pushState({}, '', '/stocks/600111?period=d1')
  sentBodies.length = 0
  convPayloads.length = 0
  clearChanSnapshotStore()
  clearChartContextStore()
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockImplementation(async () => [])
  mocks.portfolioSummary.mockReset()
  mocks.portfolioSummary.mockResolvedValue({ accounts: [], total: {} })
  installBackend()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.removeItem('token')
  clearChanSnapshotStore()
  clearChartContextStore()
})

describe('路由锚点解析（纯函数）', () => {
  it('个股/指数路由给出 symbol+market+instrument；板块/港股/脏参数一律不猜', () => {
    expect(readStockRouteAnchor('/stocks/600111', '?period=d1')).toEqual({
      symbol: '600111',
      market: 'CN',
      instrument: 'stock',
    })
    expect(readStockRouteAnchor('/stocks/000001', '?type=index')).toEqual({
      symbol: '000001',
      market: 'CN',
      instrument: 'index',
    })
    expect(readStockRouteAnchor('/stocks/BK0475', '?type=board')).toBeNull()
    expect(readStockRouteAnchor('/stocks/600111', '?market=HK')).toBeNull()
    expect(readStockRouteAnchor('/ai-copilot', '?symbol=600111')).toBeNull()
  })
})

describe('停靠面板跟随当前路由', () => {
  it('个股页打开面板：请求体带 stock_symbol/instrument/market + 图上那份 chart_snapshot', async () => {
    renderDock()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit'))
    await waitFor(() => expect(screen.getByTestId('copilot-chan-snapshot')).toBeTruthy())
    expect(screen.getByTestId('copilot-context-anchor').textContent).toContain('600111')

    await ask('这个区间的笔怎么画')
    const body = lastBody()
    expect(body.stock_symbol).toBe('600111')
    expect(body.stock_market).toBe('CN')
    expect(body.stock_instrument).toBe('stock')
    expect((body.chart_snapshot as { digest?: string })?.digest).toBe(DIGEST_A)
    // 快照不进正文、不进会话 initial_context
    expect(String(body.content)).not.toContain(DIGEST_A)
    expect(JSON.stringify(convPayloads[0])).not.toContain(DIGEST_A)
  })

  it('换股后跟随：新标的 + 新摘要，旧票的摘要一个字符都不带', async () => {
    renderDock()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit'))
    await waitFor(() => expect(screen.getByTestId('copilot-chan-snapshot')).toBeTruthy())

    fireEvent.click(screen.getByText('go-000776'))
    await waitFor(() => expect(screen.getByTestId('kline').dataset.symbol).toBe('000776:CN:stock'))
    await waitFor(() => expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull())
    expect(screen.getByTestId('copilot-context-anchor').textContent).toContain('000776')

    fireEvent.click(screen.getByText('chart-emit'))
    await waitFor(() => expect(screen.getByTestId('copilot-chan-snapshot')).toBeTruthy())

    await ask('那这只呢')
    const body = lastBody()
    expect(body.stock_symbol).toBe('000776')
    expect((body.chart_snapshot as { digest?: string })?.digest).toBe(DIGEST_B)
    expect(JSON.stringify(body)).not.toContain(DIGEST_A)
  })

  it('离开个股页 → 锚点清空，请求体只剩 content', async () => {
    renderDock()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit'))
    await waitFor(() => expect(screen.getByTestId('copilot-chan-snapshot')).toBeTruthy())

    fireEvent.click(screen.getByText('go-dashboard'))
    await waitFor(() => expect(screen.queryByTestId('copilot-context-anchor')).toBeNull())
    await waitFor(() => expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull())

    await ask('刚才那只票怎么看')
    expect(lastBody()).toEqual({ content: '刚才那只票怎么看' })
  })
})

describe('显式标的优先（路由不许覆盖用户写明的票）', () => {
  it('正文写了别的代码 → 用它，图上那份（另一只票的）快照不跟着带', async () => {
    renderDock()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit'))
    await waitFor(() => expect(screen.getByTestId('copilot-chan-snapshot')).toBeTruthy())

    await ask('000776 的线段怎么数')
    const body = lastBody()
    expect(body.stock_symbol).toBe('000776')
    expect(body.chart_snapshot).toBeUndefined()
  })

  it('从别处带着标的点开面板（entry 与当前路由不是同一只）→ entry 优先', async () => {
    renderDock({ id: 9, symbol: '601318', market: 'CN', pageContext: '用户从自选列表进入。' })
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit'))
    expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull()

    await ask('这只票怎么样')
    const body = lastBody()
    expect(body.stock_symbol).toBe('601318')
    expect(body.chart_snapshot).toBeUndefined()
  })
})
