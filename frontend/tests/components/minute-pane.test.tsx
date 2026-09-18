// @vitest-environment jsdom
//
// P1(2026-09-18): 分时面板 MinutePane —— 四种状态 + 轮询 + KJ-042 故障语义。
//
// 为什么单独测 MinutePane: 渲染 KlineChart 会真建 lightweight-charts 实例(jsdom 无 canvas),
// 而分时的**行为**全在这个面板里(取数/轮询/降级/空态)。KC 侧的接线用源级断言钉(见
// kline-minute-mode.test.tsx)。图表库本身用 mock 顶住 —— 测试只关心状态与 DOM。
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MinutePane from '@panwatch/biz-ui/components/MinutePane'

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }))
vi.mock('@panwatch/api', () => ({ fetchAPI: fetchMock }))

// 分时图组件自己也会建 lightweight-charts → 这里换成可断言的占位
vi.mock('@panwatch/biz-ui/components/MinuteLwcChart', () => ({
  default: ({ points, prevClose }: { points: unknown[]; prevClose: number | null }) => (
    <div data-testid="minute-lwc" data-points={points.length} data-prev={String(prevClose)} />
  ),
}))

const PTS = [
  { t: '09:30', price: 9.6, avg: 9.6, volume: 1000 },
  { t: '09:31', price: 9.7, avg: 9.65, volume: 2000 },
]

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.useRealTimers()
})

beforeEach(() => {
  fetchMock.mockResolvedValue({ symbol: '002361', market: 'CN', points: PTS, prev_close: 9.5 })
})

describe('MinutePane', () => {
  it('有分时点 → 渲染分时图(带上昨收基准)', async () => {
    render(<MinutePane symbol="002361" market="CN" />)
    await waitFor(() => expect(screen.getByTestId('minute-lwc')).toBeTruthy())
    expect(screen.getByTestId('minute-lwc').getAttribute('data-points')).toBe('2')
    expect(screen.getByTestId('minute-lwc').getAttribute('data-prev')).toBe('9.5')
    expect(fetchMock.mock.calls[0][0]).toContain('/quotes/minute/002361?market=CN')
  })

  it('源标 degraded → 明说"空列表是故障不等于停牌", 且不显示空态', async () => {
    fetchMock.mockResolvedValue({
      symbol: '002361',
      market: 'CN',
      points: [],
      degraded: true,
      note: '分时源连接超时',
    })
    render(<MinutePane symbol="002361" market="CN" />)
    await waitFor(() => expect(screen.getByText(/分时源异常/)).toBeTruthy())
    expect(screen.getByText(/分时源连接超时/)).toBeTruthy()
    expect(screen.getByText(/空列表是源故障，不等于停牌/)).toBeTruthy()
    expect(document.querySelector('[data-minute-empty]')).toBeNull()
  })

  it('请求失败 → 显示失败原因, 不画图也不补 0', async () => {
    fetchMock.mockRejectedValue(new Error('boom-500'))
    render(<MinutePane symbol="002361" market="CN" />)
    await waitFor(() => expect(screen.getByText(/分时加载失败：boom-500/)).toBeTruthy())
    expect(screen.queryByTestId('minute-lwc')).toBeNull()
    expect(screen.queryByText('+0.00')).toBeNull()
  })

  it('空点集(非交易日) → 显式空态说明, 不假装有数据', async () => {
    fetchMock.mockResolvedValue({ symbol: '002361', market: 'CN', points: [] })
    render(<MinutePane symbol="002361" market="CN" />)
    await waitFor(() => expect(document.querySelector('[data-minute-empty]')).toBeTruthy())
    expect(screen.getByText(/分时暂无数据/)).toBeTruthy()
  })

  it('按 pollMs 轮询(盘中 30s 刷新语义)', async () => {
    vi.useFakeTimers()
    render(<MinutePane symbol="002361" market="CN" pollMs={1000} />)
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2)
    vi.useRealTimers()
  })
})
