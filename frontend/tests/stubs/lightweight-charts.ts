/**
 * 测试用「假图表库」(B1 首屏冷启动回归, 2026-10-05)。
 *
 * 为什么需要它: `KlineChart` 的加载链回归要渲染**真组件**, 但真 `lightweight-charts`
 * 在 jsdom 里没有 canvas, 内部 rAF 绘制会抛 `Value is null`(未捕获异常污染整个用例)。
 * `lightweight-charts` 是 `packages/biz-ui` 的依赖, 不在 frontend 根 node_modules, 测试文件
 * 既 import 不到它、`vi.mock` 也拦不住(node_modules 依赖被 externalize, Vite 不过插件管线)。
 *
 * 故在 `vite.config.ts` 的 `test.alias` 里把 `lightweight-charts` 映射到本文件 ——
 * 只替换 **canvas 层**, 组件自身的取数/状态机/竞态守卫全部走真实代码。
 * 本文件零 DOM 依赖, 只提供组件会用到的 API 表面(与方法桩)。
 */

interface FakeSeries {
  setData: (data: unknown) => void
  applyOptions: (o: unknown) => void
  createPriceLine: (o: unknown) => Record<string, never>
  removePriceLine: (line: unknown) => void
  priceScale: () => { applyOptions: (o: unknown) => void }
  coordinateToPrice: () => number
  priceToCoordinate: () => null
}

function makeSeries(): FakeSeries {
  return {
    setData: () => {},
    applyOptions: () => {},
    createPriceLine: () => ({}),
    removePriceLine: () => {},
    priceScale: () => ({ applyOptions: () => {} }),
    coordinateToPrice: () => 0,
    priceToCoordinate: () => null,
  }
}

interface FakeChart {
  addSeries: () => FakeSeries
  removeSeries: () => void
  applyOptions: () => void
  remove: () => void
  panes: () => Array<{ getHeight: () => number; setStretchFactor: () => void }>
  timeScale: () => {
    fitContent: () => void
    subscribeVisibleTimeRangeChange: () => void
    subscribeVisibleLogicalRangeChange: () => void
  }
  subscribeCrosshairMove: () => void
  unsubscribeCrosshairMove: () => void
  subscribeDblClick: () => void
  setCrosshairPosition: () => void
  clearCrosshairPosition: () => void
  priceScale: () => { applyOptions: () => void }
}

export function createChart(): FakeChart {
  return {
    addSeries: () => makeSeries(),
    removeSeries: () => {},
    applyOptions: () => {},
    remove: () => {},
    panes: () => [
      { getHeight: () => 300, setStretchFactor: () => {} },
      { getHeight: () => 120, setStretchFactor: () => {} },
    ],
    timeScale: () => ({
      fitContent: () => {},
      subscribeVisibleTimeRangeChange: () => {},
      subscribeVisibleLogicalRangeChange: () => {},
    }),
    subscribeCrosshairMove: () => {},
    unsubscribeCrosshairMove: () => {},
    subscribeDblClick: () => {},
    setCrosshairPosition: () => {},
    clearCrosshairPosition: () => {},
    priceScale: () => ({ applyOptions: () => {} }),
  }
}

/** series 构造器: 组件只用它们作 `addSeries` 的入参, 无行为。 */
export const CandlestickSeries = { type: 'Candlestick' }
export const HistogramSeries = { type: 'Histogram' }
export const LineSeries = { type: 'Line' }
export const AreaSeries = { type: 'Area' }
export const BarSeries = { type: 'Bar' }
export const BaselineSeries = { type: 'Baseline' }

export function createSeriesMarkers() {
  return { setMarkers: () => {}, detach: () => {} }
}

export const LineStyle = { Solid: 0 }
export const CrosshairMode = { Magnet: 1 }
