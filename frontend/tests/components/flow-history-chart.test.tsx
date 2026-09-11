// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 2026-09-09 回归: 后端 /market-data/market-capital-flow/history 返回信封
// {hours, count, items, note}, 组件曾按数组解析 → 有数据也永远显示"刚上线暂无历史"。
// 2026-09-11 回归(老板报"图没内容"): total_main_flow 单位=**亿**(与大盘资金流卡片同源),
// 前端曾再除 1e8 → -695.9 变 -7e-06 → 曲线压平成 0。
const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  setOption: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('@panwatch/biz-ui/lib/echarts-core', () => ({
  default: {
    graphic: {
      LinearGradient: class {
        constructor() {}
      },
    },
  },
}))
vi.mock('@panwatch/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, on: vi.fn(), off: vi.fn(), resize: vi.fn() } },
  }),
}))
vi.mock('@panwatch/biz-ui/lib/stock-colors', () => ({
  readStockColors: () => ({ up: '#ef4444', down: '#22c55e' }),
  withAlpha: (c: string) => c,
}))

import FlowHistoryChart from '@panwatch/biz-ui/components/dashboard/FlowHistoryChart'

/** 最近一次 setOption 的折线数据 */
function lastSeriesData(): Array<number | null> {
  const opt = mocks.setOption.mock.calls.at(-1)?.[0] as {
    series?: Array<{ data?: Array<number | null> }>
  }
  return opt?.series?.[0]?.data ?? []
}

describe('FlowHistoryChart 响应解析', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    mocks.fetchAPI.mockReset()
    mocks.setOption.mockReset()
  })

  it('信封 {items:[...]} 有数据时渲染图表, 不显示空态', async () => {
    mocks.fetchAPI.mockResolvedValue({
      hours: 4,
      count: 2,
      items: [{ ts: '2026-09-09T10:00:00', total_main_flow: -735 }],
      note: '',
    })
    const { container } = render(<FlowHistoryChart />)
    await waitFor(() => expect(container.querySelector('.animate-pulse')).toBeNull())
    expect(screen.queryByText(/暂无历史快照|刚上线/)).toBeNull()
  })

  it('items 为空时展示后端 note', async () => {
    mocks.fetchAPI.mockResolvedValue({
      hours: 4,
      count: 0,
      items: [],
      note: '暂无快照(等待大盘资金接口写入)',
    })
    render(<FlowHistoryChart />)
    expect(await screen.findByText('暂无快照(等待大盘资金接口写入)')).toBeTruthy()
  })

  it('请求失败时提示读取失败', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('boom'))
    render(<FlowHistoryChart />)
    expect(await screen.findByText('读取失败')).toBeTruthy()
  })

  it('原样使用"亿"单位画图(不再除 1e8)', async () => {
    mocks.fetchAPI.mockResolvedValue({
      hours: 4,
      count: 3,
      note: '',
      items: [
        { ts: '2026-09-11T09:23:55', total_main_flow: 0 },
        { ts: '2026-09-11T11:01:21', total_main_flow: -636.9 },
        { ts: '2026-09-11T12:09:42', total_main_flow: -695.9 },
      ],
    })
    render(<FlowHistoryChart />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    expect(lastSeriesData()).toEqual([0, -636.9, -695.9])
  })

  it('null 值保留断点, 不编造 0', async () => {
    mocks.fetchAPI.mockResolvedValue({
      hours: 4,
      count: 2,
      note: '',
      items: [
        { ts: '2026-09-11T09:23:55', total_main_flow: null },
        { ts: '2026-09-11T11:01:21', total_main_flow: -636.9 },
      ],
    })
    render(<FlowHistoryChart />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    expect(lastSeriesData()).toEqual([null, -636.9])
  })
})
