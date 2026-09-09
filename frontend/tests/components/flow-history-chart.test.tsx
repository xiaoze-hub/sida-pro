// @vitest-environment jsdom
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// 2026-09-09 回归: 后端 /market-data/market-capital-flow/history 返回信封
// {hours, count, items, note}, 组件曾按数组解析 → 有数据也永远显示"刚上线暂无历史"。
const fetchAPIMock = vi.fn()
vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => fetchAPIMock(...args),
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
  useECharts: () => ({ ref: () => {}, chartRef: { current: { setOption: vi.fn() } } }),
}))
vi.mock('@panwatch/biz-ui/lib/stock-colors', () => ({
  readStockColors: () => ({ up: '#ef4444', down: '#22c55e' }),
  withAlpha: (c: string) => c,
}))

import FlowHistoryChart from '@panwatch/biz-ui/components/dashboard/FlowHistoryChart'

describe('FlowHistoryChart 响应解析', () => {
  beforeEach(() => {
    fetchAPIMock.mockReset()
  })

  it('信封 {items:[...]} 有数据时渲染图表, 不显示空态', async () => {
    fetchAPIMock.mockResolvedValue({
      hours: 4,
      count: 2,
      items: [{ ts: '2026-09-09T10:00:00', total_main_flow: -7_350_000_000 }],
      note: '',
    })
    const { container } = render(<FlowHistoryChart />)
    await waitFor(() => expect(container.querySelector('.animate-pulse')).toBeNull())
    expect(screen.queryByText(/暂无历史快照|刚上线/)).toBeNull()
  })

  it('items 为空时展示后端 note', async () => {
    fetchAPIMock.mockResolvedValue({
      hours: 4,
      count: 0,
      items: [],
      note: '暂无快照(等待大盘资金接口写入)',
    })
    render(<FlowHistoryChart />)
    expect(await screen.findByText('暂无快照(等待大盘资金接口写入)')).toBeTruthy()
  })

  it('请求失败时提示读取失败', async () => {
    fetchAPIMock.mockRejectedValue(new Error('boom'))
    render(<FlowHistoryChart />)
    expect(await screen.findByText('读取失败')).toBeTruthy()
  })
})
