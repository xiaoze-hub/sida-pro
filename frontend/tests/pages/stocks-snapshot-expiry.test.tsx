// @vitest-environment jsdom
/**
 * B1 / B3（2026-10-29 独立复审）· 旧时钟与旧报价必须有**独立的本地有效期**，
 * 且跨日不能靠「HH:mm 一样」自证。
 *
 * 复审复现的两条真实链路：
 *  - B1：`fetchAPI` 曾把超时计时器在**收到响应头**时清掉，响应体挂住 ⇒ `/quotes/batch` 与
 *    `/stocks/markets/status` 的 promise 永不 settle ⇒ 两个 in-flight 闸门永不释放 ⇒
 *    报价时刻与市场墙钟**一起停在 09:31**，14:00 仍判 `current`（"实时破位"）。
 *    即使上游 GET 被同键合并成一条"共享的挂死请求"，本地也必须能自己过期。
 *  - B3：昨天 15:03 的快照（`quote_date=昨天`、`daily_pnl_period=today`）在第二天
 *    09:31 的同一面钟下算出 age=0 ⇒ 旧价冒充实时。服务器市场墙钟从 15:03 **回绕**到
 *    09:31 就是"跨日"的可核对信号：此时快照的日期层必须作废，等下一次真实轮询重标。
 *
 * 这里用真实 timer 推进（vi.advanceTimersByTimeAsync），并让请求**永不 resolve**，
 * 复现"共享 GET 挂死"这一最坏情形。
 */
import { act, cleanup, render } from '@testing-library/react'
import { useRef, useState } from 'react'
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

import {
  FRESHNESS_HEARTBEAT_MS,
  IDLE_QUOTE_REFRESH_MS,
  MARKET_CLOCK_TTL_MS,
  QUOTE_SNAPSHOT_TTL_MS,
  useStocksData,
} from '@/pages/stocks/useStocksData'
import { TabActiveProvider } from '@sida/base-ui'
import type { QuoteSnapshot } from '@/pages/stocks/shared'

const noop = () => {}

const VERIFIED_QUOTE: QuoteSnapshot = {
  current_price: 33,
  change_pct: -1.5,
  quote_time: '2026-10-28 15:00:00',
  quote_date: '2026-10-28',
  daily_pnl_period: 'today',
}

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
      { id: 11, symbol: '600111', name: '北方稀土', market: 'CN', cost_price: 36.52, quantity: 100 },
    ]),
    setPortfolioRaw: noop,
    setPortfolioLoading: noop,
    setExpandedAccounts: noop,
    quotes: {},
    setQuotes: noop,
    setQuotesLoading: noop,
    klineSummaries: {},
    setKlineSummaries: noop,
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

function renderShell(state: Record<string, unknown>) {
  return render(
    <MemoryRouter>
      <TabActiveProvider active>
        <Shell state={state} />
      </TabActiveProvider>
    </MemoryRouter>,
  )
}

async function flush(rounds = 12) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
  await flush()
}

const callsOf = (frag: string) =>
  api.fetchAPI.mock.calls.filter((c) => String(c[0]).includes(frag)).length

const NEVER = () => new Promise<never>(() => {})

/**
 * 把 `setQuotes` 的调用序列（值 + 函数式更新）折成"最终行情表"，
 * 与真实 `useState` 的语义一致 —— 只取最后一次调用的参数会漏掉函数式更新。
 */
function foldQuoteState(setQuotes: ReturnType<typeof vi.fn>, initial: Record<string, QuoteSnapshot>) {
  let state: Record<string, QuoteSnapshot> = initial
  for (const call of setQuotes.mock.calls) {
    const arg = call[0]
    state = typeof arg === 'function' ? (arg as (p: Record<string, QuoteSnapshot>) => Record<string, QuoteSnapshot>)(state) : arg
  }
  return state
}

beforeEach(() => {
  api.fetchAPI.mockReset()
  stream.useQuoteStream.mockReset()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('useStocksData · 心跳不能自激（写回状态 → 重新挂表 → 立刻再请求）', () => {
  /**
   * 真实页面里 `setMarketStatus` / `setQuotes` 会写回父组件 state ⇒ 重渲染。
   * 如果心跳回调的身份跟着这些 state 变（例如把 `marketStatus` 放进 `useCallback` 依赖），
   * `usePausedInterval`（deps 变化即重新挂表、`immediate:true`）就会每写回一次状态
   * 立刻再打一轮：一个自激请求环，比原来的"冻结"更糟（既不准又打满行情源）。
   *
   * 这里用带真实 state 的容器复现这条路：请求数必须只随**定时器推进**增长。
   */
  function LiveShell() {
    // 真实页面里 `useStocksState` 的 ref 容器、`stocks`、`portfolioRaw` 都是**稳定**的
    // （useRef / state），只有 `quotes` / `marketStatus` 会随请求写回而变。
    // 脚手架必须照这个形状搭，否则自己制造的引用抖动会把"真实缺陷"淹掉。
    const refs = useRef({
      klineRefreshInFlight: { current: null },
      watchDragSnapshotRef: { current: null },
      positionDragSnapshotRef: { current: null },
    }).current
    const [stocks] = useState<never[]>([])
    const [raw] = useState(() =>
      portfolioRaw([
        { id: 11, symbol: '600111', name: '北方稀土', market: 'CN', cost_price: 36.52, quantity: 100 },
      ]),
    )
    const [marketStatus, setMarketStatus] = useState<unknown[]>([])
    const [quotes, setQuotes] = useState<Record<string, QuoteSnapshot>>({})
    useStocksData(
      makeState({
        stocks,
        portfolioRaw: raw,
        marketStatus,
        setMarketStatus,
        quotes,
        setQuotes,
        ...refs,
      }) as unknown as Parameters<typeof useStocksData>[0],
    )
    return null
  }

  it('状态写回后不额外请求：请求数只随定时器推进', async () => {
    vi.useFakeTimers()
    api.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/stocks/markets/status')) {
        return Promise.resolve([{ code: 'CN', name: 'A股', status: 'trading', local_time: '09:31' }])
      }
      if (u.includes('/quotes/batch')) {
        return Promise.resolve([
          {
            symbol: '600111',
            market: 'CN',
            current_price: 33,
            change_pct: 0,
            quote_time: '2026-10-29 09:31:00',
            quote_date: '2026-10-29',
            daily_pnl_period: 'today',
          },
        ])
      }
      if (u.includes('/portfolio/summary')) return Promise.resolve(portfolioRaw([]))
      return Promise.resolve([])
    })

    render(
      <MemoryRouter>
        <TabActiveProvider active>
          <LiveShell />
        </TabActiveProvider>
      </MemoryRouter>,
    )
    await flush()
    const baseline = callsOf('/stocks/markets/status')
    expect(baseline).toBeLessThanOrEqual(3)

    // 推进一个心跳：最多 +1（心跳本身），不许因为状态写回再连锁
    await advance(FRESHNESS_HEARTBEAT_MS)
    expect(callsOf('/stocks/markets/status') - baseline).toBeLessThanOrEqual(1)

    const afterOne = callsOf('/stocks/markets/status')
    await advance(FRESHNESS_HEARTBEAT_MS * 3)
    expect(callsOf('/stocks/markets/status') - afterOne).toBeLessThanOrEqual(3)
  })
})

describe('useStocksData · 本地有效期（不依赖请求能不能回来）', () => {
  it('市场墙钟挂死：超过 MARKET_CLOCK_TTL_MS 后清空时钟（下游判 no_clock，不冒充新鲜）', async () => {
    vi.useFakeTimers()
    let hangStatus = false
    api.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/stocks/markets/status')) {
        return hangStatus
          ? NEVER()
          : Promise.resolve([{ code: 'CN', name: 'A股', status: 'trading', local_time: '09:31' }])
      }
      if (u.includes('/quotes/batch')) {
        return Promise.resolve([
          {
            symbol: '600111',
            market: 'CN',
            current_price: 33,
            change_pct: 0,
            quote_time: '2026-10-29 09:31:00',
            quote_date: '2026-10-29',
            daily_pnl_period: 'today',
          },
        ])
      }
      if (u.includes('/portfolio/summary')) return Promise.resolve(portfolioRaw([]))
      return Promise.resolve([])
    })

    const setMarketStatus = vi.fn()
    renderShell(makeState({ setMarketStatus, quotes: { 'CN:600111': VERIFIED_QUOTE } }))
    await flush()

    hangStatus = true
    await advance(MARKET_CLOCK_TTL_MS + FRESHNESS_HEARTBEAT_MS)

    // 最后一次写入必须是空时钟（不是停在 09:31 的旧值）
    const last = setMarketStatus.mock.calls.at(-1)?.[0]
    expect(last).toEqual([])
  })

  it('报价请求挂死：超过 QUOTE_SNAPSHOT_TTL_MS 后清掉时刻/日期/期间，价格留作陈列', async () => {
    vi.useFakeTimers()
    let hangQuotes = false
    api.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/stocks/markets/status')) {
        return Promise.resolve([{ code: 'CN', name: 'A股', status: 'trading', local_time: '09:31' }])
      }
      if (u.includes('/quotes/batch')) {
        return hangQuotes
          ? NEVER()
          : Promise.resolve([
              {
                symbol: '600111',
                market: 'CN',
                current_price: 33,
                change_pct: 0,
                quote_time: '2026-10-29 09:31:00',
                quote_date: '2026-10-29',
                daily_pnl_period: 'today',
              },
            ])
      }
      if (u.includes('/portfolio/summary')) return Promise.resolve(portfolioRaw([]))
      return Promise.resolve([])
    })

    // quotes 从"上一轮成功"的快照开始，但请求已挂死（模拟共享 GET 卡住）
    const setQuotes = vi.fn()
    renderShell(makeState({ setQuotes, quotes: { 'CN:600111': VERIFIED_QUOTE } }))
    await flush()

    hangQuotes = true
    await advance(QUOTE_SNAPSHOT_TTL_MS + FRESHNESS_HEARTBEAT_MS)

    // 把 setQuotes 的调用序列折成最终行情表（旧实现：一次都不会清，时刻还停在上面）
    const final = foldQuoteState(setQuotes, { 'CN:600111': VERIFIED_QUOTE })
    expect(final['CN:600111']?.current_price).toBe(33)
    expect(final['CN:600111']?.quote_time).toBeNull()
    expect(final['CN:600111']?.quote_date).toBeNull()
    expect(final['CN:600111']?.daily_pnl_period).toBe('unknown')
  })

  it('服务器市场墙钟回绕（15:03 → 09:31）= 跨日：旧快照的日期层立即作废', async () => {
    vi.useFakeTimers()
    let clock = '15:03'
    let hangQuotes = false
    api.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/stocks/markets/status')) {
        return Promise.resolve([{ code: 'CN', name: 'A股', status: 'trading', local_time: clock }])
      }
      if (u.includes('/quotes/batch')) {
        return hangQuotes
          ? NEVER()
          : Promise.resolve([
              {
                // 昨天 15:00 的快照：后端当时判的是「today」（昨天）。
                symbol: '600111',
                market: 'CN',
                current_price: 33,
                change_pct: 0,
                quote_time: '2026-10-28 15:00:00',
                quote_date: '2026-10-28',
                daily_pnl_period: 'today',
              },
            ])
      }
      if (u.includes('/portfolio/summary')) return Promise.resolve(portfolioRaw([]))
      return Promise.resolve([])
    })

    const setQuotes = vi.fn()
    renderShell(makeState({ setQuotes, quotes: { 'CN:600111': VERIFIED_QUOTE } }))
    await flush()

    hangQuotes = true
    clock = '09:31' // 第二天开盘：同一面钟、同一分钟
    await advance(FRESHNESS_HEARTBEAT_MS)

    const cleared = foldQuoteState(setQuotes, { 'CN:600111': VERIFIED_QUOTE })
    expect(cleared['CN:600111']?.quote_time).toBeNull()
    expect(cleared['CN:600111']?.daily_pnl_period).toBe('unknown')
  })

  it('休市时降低全自选批量行情频率（时钟照刷），复用在飞请求而不是叠加', async () => {
    vi.useFakeTimers()
    api.fetchAPI.mockImplementation((url: string) => {
      const u = String(url)
      if (u.includes('/stocks/markets/status')) {
        return Promise.resolve([{ code: 'CN', name: 'A股', status: 'closed', local_time: '16:00' }])
      }
      if (u.includes('/quotes/batch')) return Promise.resolve([])
      if (u.includes('/portfolio/summary')) return Promise.resolve(portfolioRaw([]))
      return Promise.resolve([])
    })

    renderShell(
      makeState({
        marketStatus: [{ code: 'CN', status: 'closed', local_time: '16:00' }],
      }),
    )
    await flush()

    const quotesBefore = callsOf('/quotes/batch')
    const statusBefore = callsOf('/stocks/markets/status')
    await advance(FRESHNESS_HEARTBEAT_MS * 2)
    // 休市：20s 心跳不再打批量行情（旧实现每次都打）
    expect(callsOf('/quotes/batch')).toBe(quotesBefore)
    // 但市场墙钟照刷（时效判定不能没有时钟）
    expect(callsOf('/stocks/markets/status')).toBeGreaterThan(statusBefore)

    await advance(IDLE_QUOTE_REFRESH_MS)
    expect(callsOf('/quotes/batch')).toBeGreaterThan(quotesBefore)
  })
})
