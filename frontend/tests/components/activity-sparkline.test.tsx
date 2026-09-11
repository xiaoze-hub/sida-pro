// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 机构活跃度副图 (2026-09-11 决策先锋升级 C): 逐日活跃度 + 三阈值线 + 共振红点。
const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn(), setOption: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('@panwatch/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, on: vi.fn(), off: vi.fn(), resize: vi.fn() } },
  }),
}))

import ActivitySparkline from '@panwatch/biz-ui/components/ActivitySparkline'

const RESP = {
  symbol: '600519',
  available: true,
  lines: { life: 1.56, strong: 3, bull: 6 },
  count: 3,
  items: [
    { date: '20260909', activity: 0.79, level: '弱', resonance_level: '无' },
    { date: '20260910', activity: 0.66, level: '弱', resonance_level: '无' },
    { date: '20260911', activity: 1.25, level: '弱', resonance_level: '无' },
  ],
}

function lastOption(): {
  series?: Array<{ data?: Array<number | null>; markLine?: { data?: Array<{ yAxis?: number }> } }>
} {
  return (mocks.setOption.mock.calls.at(-1)?.[0] ?? {}) as never
}

describe('ActivitySparkline 机构活跃度副图', () => {
  afterEach(() => cleanup())
  beforeEach(() => {
    mocks.fetchAPI.mockReset()
    mocks.setOption.mockReset()
  })

  it('画活跃度序列 + 三条阈值线', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<ActivitySparkline symbol="600519" />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    const opt = lastOption()
    expect(opt.series?.[0]?.data).toEqual([0.79, 0.66, 1.25])
    const ys = (opt.series?.[0]?.markLine?.data ?? []).map((d) => d.yAxis)
    expect(ys).toEqual([1.56, 3, 6])
  })

  it('不可用时展示占位文案', async () => {
    mocks.fetchAPI.mockResolvedValue({ symbol: '600519', available: false, reason: '日线不足(10 根)', items: [] })
    render(<ActivitySparkline symbol="600519" />)
    expect(await screen.findByText(/日线不足/)).toBeTruthy()
  })
})
