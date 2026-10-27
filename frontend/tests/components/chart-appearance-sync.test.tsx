// @vitest-environment jsdom
import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ init: vi.fn(), registerTheme: vi.fn(), setOption: vi.fn(), dispose: vi.fn(), applyOptions: vi.fn(), remove: vi.fn(), setRange: vi.fn(), createChart: vi.fn() }))
vi.mock('@sida/biz-ui/lib/echarts-core', () => ({ default: { init: mocks.init, registerTheme: mocks.registerTheme } }))
vi.mock('lightweight-charts', () => ({ createChart: mocks.createChart, LineSeries: {}, HistogramSeries: {} }))

import { useECharts } from '@sida/biz-ui/hooks/useECharts'
import MinuteLwcChart from '@sida/biz-ui/components/MinuteLwcChart'

function Harness() { const chart = useECharts(); return <div ref={chart.ref} /> }
beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  document.documentElement.style.setProperty('--chart-canvas', '0 0% 100%')
  document.documentElement.style.setProperty('--bg-kline', '0 0% 100%')
  mocks.init.mockReturnValue({ resize: vi.fn(), getOption: () => ({ xAxis: [{ type: 'category' }], yAxis: [{ type: 'value' }] }), setOption: mocks.setOption, dispose: mocks.dispose })
  mocks.createChart.mockReturnValue({ applyOptions: mocks.applyOptions, remove: mocks.remove, addSeries: () => ({ setData: vi.fn(), applyOptions: vi.fn(), createPriceLine: vi.fn() }), timeScale: () => ({ setVisibleLogicalRange: mocks.setRange }) })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.documentElement.removeAttribute('style'); document.documentElement.removeAttribute('data-theme') })

describe('图表外观同步', () => {
  it('分时图更新画布而不重建实例或重置缩放，并在卸载时停止观察', async () => {
    const view = render(<MinuteLwcChart points={[{ t: '0930', price: 10, volume: 100, avg: 10 }]} prevClose={10} isIndex />)
    expect(mocks.createChart.mock.calls[0][1].layout.background.color).toBe('hsl(0, 0%, 100%)')
    document.documentElement.style.setProperty('--bg-kline', '228 25% 8%')
    document.documentElement.setAttribute('data-theme', 'dim')
    await waitFor(() => expect(mocks.applyOptions).toHaveBeenCalledWith(expect.objectContaining({ layout: expect.objectContaining({ background: { color: 'hsl(228, 25%, 8%)' } }) })))
    expect(mocks.createChart).toHaveBeenCalledTimes(1)
    expect(mocks.setRange).toHaveBeenCalledTimes(1)
    view.unmount()
    mocks.applyOptions.mockClear()
    document.documentElement.setAttribute('data-theme', 'other')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(mocks.applyOptions).not.toHaveBeenCalled()
    expect(mocks.remove).toHaveBeenCalledTimes(1)
  })
  it('ECharts 重新读取当前主题，保留 series 和 dataZoom 并清理实例', async () => {
    const view = render(<Harness />)
    expect(mocks.registerTheme).toHaveBeenCalledWith('sida', expect.objectContaining({ backgroundColor: 'hsl(0, 0%, 100%)' }))
    document.documentElement.style.setProperty('--chart-canvas', '228 25% 8%')
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    const patch = mocks.setOption.mock.calls.at(-1)![0]
    expect(patch.backgroundColor).toBe('hsl(228, 25%, 8%)')
    expect(patch.series).toBeUndefined()
    expect(patch.dataZoom).toBeUndefined()
    expect(mocks.init).toHaveBeenCalledTimes(1)
    expect(mocks.dispose).not.toHaveBeenCalled()
    view.unmount()
    expect(mocks.dispose).toHaveBeenCalledTimes(1)
  })
})
