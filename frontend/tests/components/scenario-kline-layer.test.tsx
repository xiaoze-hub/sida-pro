// @vitest-environment jsdom
//
// 情景关键位图层(2026-10-22)—— **组件侧接线**回归(渲染真 KlineChart)。
//
// 纯函数口径在 `scenario-lines.test.ts`; 这里钉住"图层怎么挂的":
//   - 默认**关**: 不发 /scenario 请求、图上不画线(不干扰原有视图);
//   - 开启后: 自取 `/klines/{symbol}/scenario`, 用 createPriceLine 画 2~4 条虚线;
//   - 关图层: 旧 priceLine 被清掉, 不残留在图上;
//   - available:false → 静默不画(不弹错)。
//
// 图表库在测试期被替换成无副作用的假实现(见 vite.config.ts 的 test.alias), 本用例因此
// 直接断言"画了几条线/什么价/什么 title/什么色", 而不是像素。
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

// 记录画到主图上的价位线(假图表库把 createPriceLine 记到这些数组里)。
// 用**存活集合**而不是累计计数 —— 组件每次重跑图层 effect 都是"先删旧线再画新线",
// 累计计数会把"删了又画"误判成"线条累积"(那才是真 bug)。存活集合直接反映图上现在有几条线。
const H = vi.hoisted(() => ({
  calls: [] as string[],
  live: new Map<object, Record<string, unknown>>(),
  removed: [] as unknown[],
  scenario: {} as Record<string, unknown>,
}))

vi.mock('@panwatch/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string) => {
    const u = String(url)
    H.calls.push(u)
    if (u.includes('/summary')) return Promise.resolve({})
    return Promise.resolve({
      klines: [
        { date: '2026-03-05', open: 32, high: 33, low: 31, close: 32.93, volume: 1000 },
        { date: '2026-03-06', open: 32.93, high: 34.2, low: 32.5, close: 33.5, volume: 1100 },
      ],
    })
  }),
  // 情景层专用的取数入口(失败返回 null —— 不抛错, 图层自己降级)
  fetchScenario: vi.fn((symbol: string, market: string) => {
    H.calls.push(`/klines/${symbol}/scenario?market=${market}`)
    return Promise.resolve(H.scenario)
  }),
}))

vi.mock('lightweight-charts', async () => {
  const stub = await import('../../tests/stubs/lightweight-charts')
  const base = stub as unknown as {
    createChart: () => unknown
    CandlestickSeries: unknown
    HistogramSeries: unknown
    LineSeries: unknown
    createSeriesMarkers: () => unknown
    LineStyle: Record<string, number>
    CrosshairMode: Record<string, number>
  }
  const wrappedCreateChart = () => {
    const chart = base.createChart() as {
      addSeries: () => {
        setData: () => void
        applyOptions: () => void
        createPriceLine: (o: Record<string, unknown>) => Record<string, never>
        removePriceLine: (l: unknown) => void
        priceScale: () => { applyOptions: (o: unknown) => void }
        coordinateToPrice: () => number
        priceToCoordinate: () => null
      }
    }
    const origAdd = chart.addSeries
    chart.addSeries = () => {
      const s = origAdd()
      const origCreate = s.createPriceLine
      s.createPriceLine = (o: Record<string, unknown>) => {
        const handle = origCreate(o)
        H.live.set(handle, o)
        return handle
      }
      const origRemove = s.removePriceLine
      s.removePriceLine = (l: unknown) => {
        H.removed.push(l)
        H.live.delete(l as object)
        origRemove(l)
      }
      return s
    }
    return chart
  }
  return {
    ...base,
    createChart: wrappedCreateChart,
    LineStyle: { ...base.LineStyle, Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3 },
  }
})

import KlineChart from '@panwatch/biz-ui/components/KlineChart'

/** 取"情景图层当前活在线"(title 是 阻力/支撑/走强目标/走弱目标)—— 反映图上现在有几条。 */
const scenarioLines = () =>
  [...H.live.values()].filter((l) =>
    ['阻力', '支撑', '走强目标', '走弱目标'].includes(String(l.title)),
  )

beforeEach(() => {
  H.calls.length = 0
  H.live.clear()
  H.removed.length = 0
  H.scenario = {
    available: true,
    price: 32.93,
    levels: { resist: 33.92, support: 32.92 },
    scenarios: [
      { id: 'strong', target: 45.72 },
      { id: 'weak', target: 30.5 },
    ],
  }
})

afterEach(() => {
  cleanup()
})

describe('KlineChart 情景关键位图层', () => {
  it('默认关: 不发 /scenario 请求, 图上不画情景线', async () => {
    render(<KlineChart symbol="600519" market="CN" />)
    await waitFor(() => expect(H.calls.length).toBeGreaterThan(0))
    await new Promise((r) => setTimeout(r, 30))
    expect(H.calls.some((u) => u.includes('/scenario'))).toBe(false)
    expect(scenarioLines()).toHaveLength(0)
  })

  it('layersVisible.scenario=true: 自取 /scenario 并画 4 条虚线(买红卖绿)', async () => {
    render(<KlineChart symbol="600519" market="CN" layersVisible={{ scenario: true }} />)
    await waitFor(() => expect(scenarioLines()).toHaveLength(4), { timeout: 5000 })

    expect(H.calls.some((u) => u.includes('/klines/600519/scenario'))).toBe(true)

    const lines = scenarioLines()
    for (const l of lines) {
      expect(l.lineStyle).toBe(2) // LineStyle.Dashed
      expect(l.axisLabelVisible).toBe(true)
      expect(['阻力', '支撑', '走强目标', '走弱目标']).toContain(l.title)
    }
    // 阻力/走强目标=红系, 支撑/走弱目标=绿系(同系同色, 跨系不同色)
    const byTitle = Object.fromEntries(lines.map((l) => [l.title, l.color]))
    expect(byTitle['阻力']).toBe(byTitle['走强目标'])
    expect(byTitle['支撑']).toBe(byTitle['走弱目标'])
    expect(byTitle['阻力']).not.toBe(byTitle['支撑'])
  })

  it('图下有情景关键位图例(一行式小字, 非卡片)', async () => {
    render(<KlineChart symbol="600519" market="CN" layersVisible={{ scenario: true }} />)
    await waitFor(() => expect(scenarioLines()).toHaveLength(4), { timeout: 5000 })
    const legend = await screen.findByTestId('scenario-legend')
    expect(legend.textContent).toContain('情景关键位')
    expect(legend.textContent).toContain('阻力')
    expect(legend.textContent).toContain('支撑')
    // 非卡片: 无 card 容器样式
    expect(legend.className).not.toMatch(/rounded-lg border/)
  })

  it('available:false → 静默不画(不弹错, 不画线, 无图例)', async () => {
    H.scenario = { available: false, reason: '关键位不足（缺均线/筹码带/区间高低）' }
    render(<KlineChart symbol="600519" market="CN" layersVisible={{ scenario: true }} />)
    await waitFor(() => {
      expect(H.calls.some((u) => u.includes('/scenario'))).toBe(true)
    })
    await new Promise((r) => setTimeout(r, 50))
    expect(scenarioLines()).toHaveLength(0)
    expect(screen.queryByTestId('scenario-legend')).toBeNull()
  })

  it('关掉图层 → 旧 priceLine 被清掉(不残留在图上)', async () => {
    const { rerender } = render(
      <KlineChart symbol="600519" market="CN" layersVisible={{ scenario: true }} />,
    )
    await waitFor(() => expect(scenarioLines()).toHaveLength(4), { timeout: 5000 })

    rerender(<KlineChart symbol="600519" market="CN" layersVisible={{ scenario: false }} />)
    await waitFor(() => expect(scenarioLines()).toHaveLength(0), { timeout: 5000 })
    // 旧线确实被 removePriceLine 清过(不是"换了个 id"的假象)
    expect(H.removed.length).toBeGreaterThan(0)
  })
})