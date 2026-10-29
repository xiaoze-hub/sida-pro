// @vitest-environment jsdom
/**
 * 个股工作台 → 图表上下文**发布端**（P2, 2026-10-29）。
 *
 * 这里渲染**真实 StockWorkbench**（K 线组件换成可观测替身，只把 canvas 层与取数噪声挡掉），
 * 证明上下文取自「页面真的显示着的那张图」，而不是页面自己拍脑袋：
 *   ① 周期只来自 URL `?period=`；URL 没写就不是"日线"，而是**不写周期**；
 *   ② 可视区间只来自 `KlineChart.onRangeStats` 的真实读数；
 *   ③ 价位只来自服务端已确认的标注行（`useWorkbenchAnnotations` 的 snapshot.lines）；
 *   ④ 换周期 → 旧区间立即作废（组件还没重新上报之前不许拿旧读数当新周期）；
 *   ⑤ 换标的 → 旧标的的上下文立刻取消发布（不残留、不串票）；
 *   ⑥ 离开页面 → 取消发布。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  portfolioSummary: vi.fn(),
  /** 区间读数替身：用例显式点火（模拟用户缩放/拖拽后图表上报）。 */
  emitRange: null as null | ((stats: unknown) => void),
  annotations: {
    key: '',
    phase: 'ready',
    lines: [
      { id: 1, symbol: '600111', market: 'CN', period: 'd1', price: 12.34, kind: 'support', label: '底仓成本', user_id: 'test-user', action_id: 5, ts: '', note: '', color: '', source: 'copilot', created_at: '' },
      { id: 2, symbol: '600111', market: 'CN', period: 'd1', price: 15.2, kind: 'pressure', label: '压力', user_id: 'test-user', action_id: 6, ts: '', note: '', color: '', source: 'copilot', created_at: '' },
    ] as Array<Record<string, unknown>>,
  },
}))

vi.mock('@sida/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@sida/api')>()
  return {
    ...actual,
    fetchAPI: mocks.fetchAPI,
    dashboardApi: { ...actual.dashboardApi, portfolioSummary: mocks.portfolioSummary },
  }
})

/** K 线替身: 记录周期/symbol, 并暴露一个显式点火按钮把「图表真实上报」交回页面。 */
vi.mock('@sida/biz-ui/components/KlineChart', async () => {
  const { useEffect } = await import('react')
  function KlineStub(p: {
    symbol: string
    market: string
    initialInterval?: string
    onRangeStats?: (s: unknown) => void
    onIntervalChange?: (i: string) => void
  }) {
    useEffect(() => {
      mocks.emitRange = (stats) => p.onRangeStats?.(stats)
    }, [p])
    return (
      <div data-testid="kline" data-interval={p.initialInterval} data-symbol={`${p.symbol}:${p.market}`}>
        <button type="button" onClick={() => p.onIntervalChange?.('1w')}>
          mock-to-week
        </button>
        <button type="button" onClick={() => p.onIntervalChange?.('1d')}>
          mock-to-day
        </button>
      </div>
    )
  }
  return { default: KlineStub }
})

vi.mock('@/hooks/useWorkbenchAnnotations', () => ({
  useWorkbenchAnnotations: () => ({ snapshot: { ...mocks.annotations }, priceLines: undefined }),
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

import StockWorkbench from '@/pages/StockWorkbench'
import { clearChartContextStore, readChartContext } from '@/lib/copilot/chart-context'

const OWNER = 'test-user'

/** 图表真实上报的区间读数（形状 = `KlineRangeStats`）。 */
function stats(from: string, to: string, bars = 58) {
  return { from, to, bars, firstClose: 10, lastClose: 11.2, changePct: 12, amplitudePct: 20, mingNet: null, mingDays: 0, darkNet: null, darkDays: 0, eventCounts: {}, eventTotal: 0, priceLines: [] }
}

function Probe() {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate('/stocks/000776?period=d1')}>mock-go-other-symbol</button>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Probe />
      <Routes>
        <Route path="/stocks/:symbol" element={<StockWorkbench />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** 等页面把「当前图」发布出来。 */
async function waitForContext(symbol: string) {
  await waitFor(() => expect(readChartContext(symbol, 'CN', OWNER)).not.toBeNull())
  return readChartContext(symbol, 'CN', OWNER)!
}

beforeEach(() => {
  localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: OWNER }))}.signature`)
  clearChartContextStore()
  mocks.emitRange = null
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockImplementation(async () => [])
  mocks.portfolioSummary.mockReset()
  mocks.portfolioSummary.mockResolvedValue({ accounts: [], total: {} })
})

afterEach(() => {
  cleanup()
  localStorage.removeItem('token')
  clearChartContextStore()
})

describe('发布真实视图', () => {
  it('?!period 缺失 → 不写周期（不拿图表默认值冒充用户选择）', async () => {
    renderAt('/stocks/600111')
    const ctx = await waitForContext('600111')
    expect(ctx.period).toBeUndefined()
    expect(ctx.instrument).toBe('stock')
    expect(ctx.market).toBe('CN')
  })

  it('?period=w1 → 周期按 URL 原样带上；区间未上报前不写区间', async () => {
    renderAt('/stocks/600111?period=w1')
    const ctx = await waitForContext('600111')
    expect(ctx.period).toBe('w1')
    expect(ctx.range).toBeUndefined()
  })

  it('图表上报可视区间 → 逐字进入上下文（含根数/涨跌幅/振幅）', async () => {
    renderAt('/stocks/600111?period=d1')
    await waitForContext('600111')
    act(() => mocks.emitRange?.(stats('2026-06-01', '2026-10-29', 58)))
    await waitFor(() => expect(readChartContext('600111', 'CN', OWNER)?.range).toEqual({
      from: '2026-06-01',
      to: '2026-10-29',
      bars: 58,
      changePct: 12,
      amplitudePct: 20,
    }))
  })

  it('已确认标注价位 → 进入上下文；没确认行时不写价位', async () => {
    renderAt('/stocks/600111?period=d1')
    const ctx = await waitForContext('600111')
    expect(ctx.levels).toEqual([
      { price: 12.34, kind: 'support', label: '底仓成本' },
      { price: 15.2, kind: 'pressure', label: '压力' },
    ])
    mocks.annotations.lines = []
    cleanup()
    clearChartContextStore()
    renderAt('/stocks/600111?period=d1')
    expect((await waitForContext('600111')).levels).toBeUndefined()
    mocks.annotations.lines = [
      { id: 1, symbol: '600111', market: 'CN', period: 'd1', price: 12.34, kind: 'support', label: '底仓成本', user_id: 'test-user', action_id: 5, ts: '', note: '', color: '', source: 'copilot', created_at: '' },
    ]
  })
})

describe('切换即隔离', () => {
  it('换周期 → 旧区间立刻作废（新读数到达前不许沿用）', async () => {
    renderAt('/stocks/600111?period=d1')
    await waitForContext('600111')
    act(() => mocks.emitRange?.(stats('2026-06-01', '2026-10-29')))
    await waitFor(() => expect(readChartContext('600111', 'CN', OWNER)?.range?.from).toBe('2026-06-01'))
    fireEvent.click(screen.getByText('mock-to-week'))
    await waitFor(() => expect(readChartContext('600111', 'CN', OWNER)?.period).toBe('w1'))
    expect(readChartContext('600111', 'CN', OWNER)?.range).toBeUndefined()
    // 新周期的新读数到来后，可用的是新的那一段
    act(() => mocks.emitRange?.(stats('2026-09-01', '2026-10-29', 9)))
    await waitFor(() => expect(readChartContext('600111', 'CN', OWNER)?.range?.from).toBe('2026-09-01'))
  })

  it('换标的 → 旧标的的上下文立刻取消发布', async () => {
    renderAt('/stocks/600111?period=d1')
    await waitForContext('600111')
    act(() => mocks.emitRange?.(stats('2026-06-01', '2026-10-29')))
    fireEvent.click(screen.getByText('mock-go-other-symbol'))
    await waitFor(() => expect(readChartContext('000776', 'CN', OWNER)).not.toBeNull())
    expect(readChartContext('600111', 'CN', OWNER)).toBeNull()
    // 新标的还没有图表读数 → 区间不跟着搬过去
    expect(readChartContext('000776', 'CN', OWNER)?.range).toBeUndefined()
  })

  it('指数视图不发布（不是工作台那张个股图，也不存在"同号"猜测）', async () => {
    renderAt('/stocks/000001?type=index')
    await waitFor(() => expect(screen.getByTestId('index-body')).toBeTruthy())
    expect(readChartContext('000001', 'CN', OWNER)).toBeNull()
  })
})
