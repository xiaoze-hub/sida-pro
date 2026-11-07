// @vitest-environment jsdom
//
// 指数图 K 线主图 · 取数边界拦截(2026-11-07)
//
// 背景(实测): 后端只有**主图**这一条 K 线有指数口径
// (`GET /klines/{s}?...&instrument=index` → 腾讯 sh000001 / 新浪兜底); 而
// summary / patterns / wyckoff / chart-notes / scenario / trend-line / niuxiong /
// minute-breakthrough / pool-flow-series 这些图层端点**只按 symbol 解析** —— 对 `000001`
// 它们返回的是**平安银行**(个股日表)。指数图若照发这些请求, 指数蜡烛上就会叠个股的
// GS 买卖点/形态/操盘线/牛熊线/主力意图/暗盘资金 —— 图上标的是"上证指数", 数据却是别的标的。
//
// 所以本用例渲染**真组件**(canvas 层由 tests/stubs/lightweight-charts.ts 替掉, 取数/状态机
// 全走真实代码), 在 `@sida/api::fetchAPI` 这层拦截**每一条真实请求**, 钉两件事:
//  ① 指数图: 只发主图那一条, 且必带 `instrument=index`; 个股图层端点一条都不许发;
//     父组件塞进来的个股图层也不许画(数据错标比空着更坏);
//  ② 个股图: 图层请求照旧(一条不少, 且不带 instrument) —— 本次改动不碰个股功能。
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// jsdom 无 ResizeObserver(KlineChart 容器尺寸自适应用) → 桩掉
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

const H = vi.hoisted(() => ({
  /** 每次 fetchAPI 的 url(顺序即请求次序) */
  calls: [] as string[],
  /** 主图 K 线响应(按 symbol 分键, 用例可改) */
  main: new Map<string, unknown>(),
  /** 置为该 symbol 时主图请求抛错(空态/错误态用例) */
  failSymbol: '' as string,
}))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchScenario: vi.fn(async () => ({ available: false })),
  fetchAPI: vi.fn(async (url: string) => {
    const u = String(url)
    H.calls.push(u)
    if (/^\/klines\/[^/?]+\?/.test(u)) {
      const sym = /^\/klines\/([^/?]+)/.exec(u)?.[1] ?? ''
      if (H.failSymbol && sym === H.failSymbol) throw new Error(`指数K线不可用(${sym})`)
      return H.main.get(sym) ?? { klines: [] }
    }
    // 慢车道图层端点: 一律空载荷(用例只关心**有没有发**、带没带 instrument)
    return {}
  }),
}))

import KlineChart from '@sida/biz-ui/components/KlineChart'

/** 近 5 根可用日 K(字段有限值, 过组件内的 null/NaN 过滤) */
function bars(n = 5) {
  return Array.from({ length: n }, (_, i) => ({
    date: `2026-10-${String(i + 1).padStart(2, '0')}`,
    open: 3900 + i,
    high: 3910 + i,
    low: 3890 + i,
    close: 3905 + i,
    volume: 2.1e8 + i,
  }))
}

/** 慢车道(SUMMARY_SLOW_LANE_DELAY_MS = 350ms)全跑完所需的等待 */
async function settleSlowLane() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 700))
  })
}

/** 个股图层端点(只有个股口径的那些) */
const STOCK_ONLY_ENDPOINTS = [
  '/summary',
  '/patterns',
  '/wyckoff',
  '/chart-notes',
  '/scenario',
  '/indicators/trend-line/',
  '/indicators/niuxiong/',
  '/indicators/minute-breakthrough/',
  '/pool-flow-series',
]

function stockOnlyCalls() {
  return H.calls.filter((u) => STOCK_ONLY_ENDPOINTS.some((ep) => u.includes(ep)))
}

beforeEach(() => {
  H.calls.length = 0
  H.main.clear()
  H.failSymbol = ''
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('KlineChart · 指数图(instrument=index)的取数边界', () => {
  it('只发主图一条请求, 且必带 instrument=index —— 个股图层端点一条都不发', async () => {
    H.main.set('000001', { klines: bars() })
    render(
      <KlineChart symbol="000001" market="CN" instrument="index" initialInterval="1d" initialDays={120} height={420} />,
    )

    // 首帧: 只有主图 K 线这一条
    expect(H.calls).toEqual(['/klines/000001?market=CN&days=120&interval=1d&instrument=index'])

    await waitFor(() => expect(screen.getByText('5 根K线')).toBeTruthy())
    await settleSlowLane() // 慢车道窗口过去后仍然不许发出任何个股图层请求

    expect(H.calls).toEqual(['/klines/000001?market=CN&days=120&interval=1d&instrument=index'])
    expect(stockOnlyCalls()).toEqual([])
  })

  it('父组件塞进来的个股图层也不画(GS/形态/操盘线都不上图)', async () => {
    H.main.set('000001', { klines: bars() })
    const { container } = render(
      <KlineChart
        symbol="000001"
        market="CN"
        instrument="index"
        initialInterval="1d"
        initialDays={120}
        height={420}
        gsSignals={[
          { date: '2026-10-02', side: 'G' },
          { date: '2026-10-04', side: 'S' },
        ]}
        patterns={[{ date: '2026-10-03', name: '红三兵', direction: 'bullish' } as never]}
        trendLine={{ available: true, lines: { red: [], yellow: [], green: [] }, params: {} } as never}
      />,
    )
    await waitFor(() => expect(screen.getByText('5 根K线')).toBeTruthy())
    await settleSlowLane()

    // 根容器把 GS 实际数量挂在 data-gs-count 上(巡检钩子) —— 指数图必须为 0
    expect(container.firstElementChild?.getAttribute('data-gs-count')).toBe('0')
    expect(screen.queryByTestId('gs-legend')).toBeNull()
    expect(screen.queryByTestId('pattern-legend')).toBeNull()
    expect(screen.queryByTestId('trend-line-legend')).toBeNull()
    expect(screen.queryByTestId('main-intent-legend')).toBeNull()
    expect(stockOnlyCalls()).toEqual([])
  })

  it('个股专属副图/工具入口不给: 只留成交量/MACD, 无活跃度/暗盘资金/筹码分布', async () => {
    H.main.set('000001', { klines: bars() })
    render(
      <KlineChart symbol="000001" market="CN" instrument="index" initialInterval="1d" initialDays={120} height={420} />,
    )
    expect(screen.getByText('成交量')).toBeTruthy()
    expect(screen.getByText('MACD')).toBeTruthy()
    expect(screen.queryByText('活跃度')).toBeNull()
    expect(screen.queryByText('暗盘资金')).toBeNull()
    expect(screen.queryByText(/筹码分布/)).toBeNull()
    expect(screen.queryByTestId('scenario-layer-toggle')).toBeNull()
    expect(screen.queryByTestId('minute-breakthrough-bar')).toBeNull()
  })

  it('取不到指数 K 线 → 原生空态(不画 0 线, 不拿个股顶上)', async () => {
    H.main.set('000001', { klines: [] })
    render(
      <KlineChart symbol="000001" market="CN" instrument="index" initialInterval="1d" initialDays={120} height={420} />,
    )
    await waitFor(() => expect(screen.getByText('无数据')).toBeTruthy())

    cleanup()
    H.calls.length = 0
    H.failSymbol = '000001'
    render(
      <KlineChart symbol="000001" market="CN" instrument="index" initialInterval="1d" initialDays={120} height={420} />,
    )
    await waitFor(() => expect(screen.getByText('错误: 指数K线不可用(000001)')).toBeTruthy())
    expect(screen.queryByText(/根K线/)).toBeNull()
  })
})

describe('KlineChart · 个股图行为不变(本次改动的回归闸)', () => {
  it('个股仍自发全部图层请求, 且不带 instrument 参数', async () => {
    H.main.set('600519', { klines: bars() })
    render(
      <KlineChart symbol="600519" market="CN" initialInterval="1d" initialDays={120} height={420} />,
    )
    expect(H.calls).toEqual(['/klines/600519?market=CN&days=120&interval=1d'])

    await waitFor(() => expect(screen.getByText('5 根K线')).toBeTruthy())
    await waitFor(() => expect(H.calls.some((u) => u.includes('/summary'))).toBe(true))
    await settleSlowLane()

    const urls = H.calls
    for (const ep of ['/summary', '/patterns', '/wyckoff', '/chart-notes', '/indicators/trend-line/600519', '/indicators/niuxiong/600519', '/indicators/minute-breakthrough/600519']) {
      expect(urls.some((u) => u.includes(ep))).toBe(true)
    }
    // 个股请求一条都不许带 instrument(带上是后端口径切换, 不属于本次范围)
    expect(urls.every((u) => !u.includes('instrument='))).toBe(true)
    // 个股副图/工具入口照旧
    expect(screen.getByText('活跃度')).toBeTruthy()
    expect(screen.getByText('暗盘资金')).toBeTruthy()
    expect(screen.getByText(/筹码分布/)).toBeTruthy()
  })
})
