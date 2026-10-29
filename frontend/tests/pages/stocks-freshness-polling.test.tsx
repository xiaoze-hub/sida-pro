// @vitest-environment jsdom
/**
 * 持仓页 · 行情/市场墙钟续期与 autoRefresh 解耦（2026-10-29 独立复审 F1）。
 *
 * 旧实现把 `/stocks/markets/status` 与 `/quotes/batch` 的周期刷新**只**挂在
 * `usePausedInterval(autoRefreshTick, autoRefresh ? refreshInterval*1000 : 0)` 上，
 * 而 `autoRefresh` 默认 **false**（`sida_stocks_autoRefresh`）⇒ 默认配置下两边一次都不再刷新：
 *   - 09:31 拉到的报价与 09:31 的服务器市场墙钟**一起冻结**；
 *   - 「报价距市场时钟多久」永远算出 0 分钟 ⇒ 14:00 仍把 9:31 的价判成 `current`；
 *   - 持仓风险/防线告警于是拿一上午前的价位说"你现在破位了"。
 *
 * 现在的口径（保守安全策略）：
 *   - 时效心跳**独立于**用户 autoRefresh 开关（关掉自动刷新 ≠ 允许行情与市场时钟冻结）；
 *   - 心跳同时刷市场墙钟与报价（只刷一个同样会让"距今多久"失真）；
 *   - 页签隐藏停表、回前台补一次；卸载清定时器；同一批量接口不并发打第二轮。
 *
 * 用真实 timer 推进（vi.advanceTimersByTimeAsync）验证周期，不靠 sleep。
 */
import { act, cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))
const stream = vi.hoisted(() => ({ useQuoteStream: vi.fn() }))

vi.mock('@sida/api', () => ({
  fetchAPI: api.fetchAPI,
  stocksApi: {},
  normalizeNewsEnvelope: (raw: unknown) => ({ items: [], total: 0, raw }),
}))
vi.mock('@/realtime/useQuoteStream', () => ({ useQuoteStream: stream.useQuoteStream }))
vi.mock('@sida/base-ui/components/ui/toast', () => ({ useToast: () => ({ toast: vi.fn() }) }))

import { FRESHNESS_HEARTBEAT_MS, useStocksData } from '@/pages/stocks/useStocksData'
import { TabActiveProvider } from '@sida/base-ui'

const noop = () => {}

/** 一个持仓行的最小 portfolioRaw（buildQuoteItems 需要它才发 /quotes/batch）。 */
function portfolioRaw(positions: unknown[]) {
  return {
    accounts: [
      {
        id: 1,
        name: '主账户',
        available_funds: 0,
        total_market_value: 0,
        total_cost: 0,
        total_pnl: 0,
        total_pnl_pct: 0,
        total_daily_pnl: 0,
        daily_pnl_period: 'unknown',
        daily_pnl_label: '当日盈亏',
        daily_pnl_date: null,
        total_assets: 0,
        positions,
      },
    ],
    total: {
      total_market_value: 0,
      total_cost: 0,
      total_pnl: 0,
      total_pnl_pct: 0,
      total_daily_pnl: 0,
      daily_pnl_period: 'unknown',
      daily_pnl_label: '当日盈亏',
      daily_pnl_date: null,
      available_funds: 0,
      total_assets: 0,
    },
  }
}

function makeState(over: Record<string, unknown> = {}) {
  return {
    stocks: [],
    setStocks: noop,
    setAccounts: noop,
    agents: [],
    setAgents: noop,
    setServices: noop,
    setChannels: noop,
    setLoading: noop,
    setLoadError: noop,
    portfolio: null,
    setPortfolio: noop,
    portfolioRaw: portfolioRaw([
      { id: 11, symbol: '600111', name: '北方稀土', market: 'CN', cost_price: 1, quantity: 1 },
    ]),
    setPortfolioRaw: noop,
    setPortfolioLoading: noop,
    setExpandedAccounts: noop,
    quotes: {},
    setQuotes: noop,
    setQuotesLoading: noop,
    klineSummaries: {},
    setKlineSummaries: noop,
    // 关键：默认配置（自动刷新关闭）
    autoRefresh: false,
    refreshInterval: 30,
    setLastRefreshTime: noop,
    setScanning: noop,
    setPoolSuggestions: noop,
    setPoolSuggestionsLoading: noop,
    setPriceAlertSummaryMap: noop,
    setNewsDialogOpen: noop,
    setNewsDialogSymbol: noop,
    setNews: noop,
    setNewsLoading: noop,
    setKlineDialogOpen: noop,
    setKlineDialogSymbol: noop,
    setKlineDialogMarket: noop,
    setKlineDialogName: noop,
    setKlineDialogHasPosition: noop,
    setKlineDialogInitialSummary: noop,
    setMinuteDialogOpen: noop,
    setMinuteDialogSymbol: noop,
    setMinuteDialogMarket: noop,
    setMinuteDialogName: noop,
    setStockCtxMenu: noop,
    marketStatus: [],
    setMarketStatus: noop,
    klineRefreshInFlight: { current: null },
    agentDialogStock: null,
    setDeepAnalysisTarget: noop,
    schedulePreviewCache: {},
    setSchedulePreviewCache: noop,
    schedulePreviewLoading: {},
    setSchedulePreviewLoading: noop,
    watchDragSnapshotRef: { current: null },
    positionDragSnapshotRef: { current: null },
    ...over,
  }
}

function Shell({ state }: { state: Record<string, unknown> }) {
  useStocksData(state as unknown as Parameters<typeof useStocksData>[0])
  return null
}

function renderHookShell(state: Record<string, unknown>, active = true) {
  const view = render(
    <MemoryRouter>
      <TabActiveProvider active={active}>
        <Shell state={state} />
      </TabActiveProvider>
    </MemoryRouter>,
  )
  const rerenderWith = (nextActive: boolean, nextState: Record<string, unknown> = state) =>
    view.rerender(
      <MemoryRouter>
        <TabActiveProvider active={nextActive}>
          <Shell state={nextState} />
        </TabActiveProvider>
      </MemoryRouter>,
    )
  return { view, rerenderWith }
}

async function flush(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

const callsOf = (frag: string) =>
  api.fetchAPI.mock.calls.filter((c) => String(c[0]).includes(frag)).length

let clockMinutes = 9 * 60 + 31
let quotePrice = 33

beforeEach(() => {
  api.fetchAPI.mockReset()
  stream.useQuoteStream.mockReset()
  clockMinutes = 9 * 60 + 31
  quotePrice = 33
  api.fetchAPI.mockImplementation((url: string, init?: { method?: string; body?: string }) => {
    const u = String(url)
    if (u.includes('/stocks/markets/status')) {
      const h = Math.floor(clockMinutes / 60)
      const m = clockMinutes % 60
      return Promise.resolve([
        { code: 'CN', name: 'A股', status: 'trading', status_text: '交易中', is_trading: true, sessions: [], local_time: `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}` },
      ])
    }
    if (u.includes('/quotes/batch')) {
      return Promise.resolve([
        { symbol: '600111', market: 'CN', current_price: quotePrice, change_pct: 0, quote_time: '2026-10-29 09:31:00', quote_date: '2026-10-29', daily_pnl_period: 'today' },
      ])
    }
    if (u.includes('/portfolio/summary')) return Promise.resolve(portfolioRaw([]))
    if (u.includes('/stocks')) return Promise.resolve([])
    if (u.includes('/accounts')) return Promise.resolve([])
    if (u.includes('/suggestions')) return Promise.resolve({})
    if (u.includes('/price-alerts')) return Promise.resolve([])
    if (u.includes('/klines/')) return Promise.resolve({ summary: null })
    void init
    return Promise.resolve([])
  })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('useStocksData · 时效心跳与 autoRefresh 解耦', () => {
  it('autoRefresh 关闭（默认）时，行情与市场墙钟仍按固定节奏前进', async () => {
    vi.useFakeTimers()
    renderHookShell(makeState())
    await flush()

    const quoteBefore = callsOf('/quotes/batch')
    const statusBefore = callsOf('/stocks/markets/status')
    expect(quoteBefore).toBeGreaterThan(0)
    expect(statusBefore).toBeGreaterThan(0)

    // 时间推进：服务器市场墙钟走到 14:00（报价时刻仍停 09:31）
    clockMinutes = 14 * 60
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FRESHNESS_HEARTBEAT_MS)
    })
    await flush()

    // 旧实现：autoRefresh=false ⇒ 两边一次都不再请求
    expect(callsOf('/quotes/batch')).toBeGreaterThan(quoteBefore)
    expect(callsOf('/stocks/markets/status')).toBeGreaterThan(statusBefore)
  })

  it('心跳把新的市场墙钟写回状态（不是只请求不落地）', async () => {
    vi.useFakeTimers()
    const setMarketStatus = vi.fn()
    renderHookShell(makeState({ setMarketStatus }))
    await flush()

    clockMinutes = 14 * 60
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FRESHNESS_HEARTBEAT_MS)
    })
    await flush()

    const last = setMarketStatus.mock.calls.at(-1)?.[0] as Array<{ local_time: string }>
    expect(last?.[0]?.local_time).toBe('14:00')
  })

  it('没有任何需要报价的标的时，市场墙钟照样续期（时效判定不能没有时钟）', async () => {
    vi.useFakeTimers()
    renderHookShell(makeState({ stocks: [], portfolioRaw: portfolioRaw([]) }))
    await flush()

    const statusBefore = callsOf('/stocks/markets/status')
    clockMinutes = 14 * 60
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FRESHNESS_HEARTBEAT_MS)
    })
    await flush()
    expect(callsOf('/stocks/markets/status')).toBeGreaterThan(statusBefore)
  })

  it('同一批量接口不并发：上一轮未回就不打下一轮，回完再按节奏继续', async () => {
    vi.useFakeTimers()
    let release: ((v: unknown) => void) | null = null
    let blocking = true
    api.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/quotes/batch')) {
        if (blocking) {
          blocking = false
          return new Promise((resolve) => {
            release = resolve
          })
        }
        return Promise.resolve([])
      }
      if (u.includes('/stocks/markets/status')) return Promise.resolve([])
      if (u.includes('/portfolio/summary')) return Promise.resolve(portfolioRaw([]))
      return Promise.resolve([])
    })

    renderHookShell(makeState())
    await flush()

    expect(callsOf('/quotes/batch')).toBe(1)
    // 挂起期间连推三轮：仍然只有 1 个在飞
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FRESHNESS_HEARTBEAT_MS * 3)
    })
    await flush()
    expect(callsOf('/quotes/batch')).toBe(1)

    // 放行 → 下一轮到来时可以再发
    await act(async () => {
      release?.([])
      await Promise.resolve()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FRESHNESS_HEARTBEAT_MS)
    })
    await flush()
    expect(callsOf('/quotes/batch')).toBe(2)
  })

  it('在飞期间标的集合变了 → 当前轮结束后立刻按新集合补一轮（不并发、也不丢更新）', async () => {
    vi.useFakeTimers()
    let release: ((v: unknown) => void) | null = null
    let blocking = true
    const bodies: string[] = []
    api.fetchAPI.mockImplementation((url: string, init?: { body?: string }) => {
      const u = String(url)
      if (u.includes('/quotes/batch')) {
        bodies.push(String(init?.body ?? ''))
        if (blocking) {
          blocking = false
          return new Promise((resolve) => {
            release = resolve
          })
        }
        return Promise.resolve([])
      }
      if (u.includes('/stocks/markets/status')) return Promise.resolve([])
      if (u.includes('/portfolio/summary')) return Promise.resolve(portfolioRaw([]))
      return Promise.resolve([])
    })

    const base = makeState()
    const { rerenderWith } = renderHookShell(base)
    await flush()
    expect(bodies.length).toBe(1)

    // 新增一只自选（标的集合变了）
    rerenderWith(
      true,
      makeState({ stocks: [{ id: 5, symbol: '000001', name: '平安银行', market: 'CN', agents: [] }] }),
    )
    await flush()
    // 仍不并发
    expect(bodies.length).toBe(1)

    // 放行第一轮 → 立刻补第二轮，且带上了新标的
    await act(async () => {
      release?.([])
      await Promise.resolve()
    })
    await flush()
    expect(bodies.length).toBe(2)
    expect(bodies[1]).toContain('000001')
  })

  it('隐藏页签停表，回前台立即补一次', async () => {
    vi.useFakeTimers()
    const { rerenderWith } = renderHookShell(makeState(), false)
    await flush()

    const hiddenQuotes = callsOf('/quotes/batch')
    const hiddenStatus = callsOf('/stocks/markets/status')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FRESHNESS_HEARTBEAT_MS * 3)
    })
    await flush()
    expect(callsOf('/quotes/batch')).toBe(hiddenQuotes)
    expect(callsOf('/stocks/markets/status')).toBe(hiddenStatus)

    rerenderWith(true)
    await flush()
    expect(callsOf('/quotes/batch')).toBeGreaterThan(hiddenQuotes)
    expect(callsOf('/stocks/markets/status')).toBeGreaterThan(hiddenStatus)
  })

  it('卸载后不再请求（定时器清理干净）', async () => {
    vi.useFakeTimers()
    const { view } = renderHookShell(makeState())
    await flush()
    const before = callsOf('/quotes/batch') + callsOf('/stocks/markets/status')
    view.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(FRESHNESS_HEARTBEAT_MS * 5)
    })
    expect(callsOf('/quotes/batch') + callsOf('/stocks/markets/status')).toBe(before)
  })
})
