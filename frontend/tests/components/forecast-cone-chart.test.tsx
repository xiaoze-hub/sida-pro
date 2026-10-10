// @vitest-environment jsdom
//
// 回归(空 K 线 RangeError): `ForecastConeChart` 在历史 K 线为空(拉取失败 / 新股无 K 线)时,
// 旧代码 `new Array(histVals.length - 1)` = `new Array(-1)` 抛
// `RangeError: Invalid array length` → 锥图整块白屏。本文件钉死两处边界:
//   ① 空 K 线: 不抛, 显式「无历史K线」空态;
//   ② 单根 K 线: 不抛, 正常渲染图表(非空态; histVals.length-1 = 0 → new Array(0) 合法)。
//
// 关键: `useECharts` 被替成**预置 `chartRef.current`** 的替身 —— 这样第二段 effect 会真的
// 进入 `chart.setOption(...)` 分支(生产里空态会早退不挂图, 但 effect 仍会跑), 从而在
// hist=[] 时精确复现旧代码的 `new Array(-1)` 路径。若把 Math.max 兜底删掉, ① 会立刻失败。
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  setOption: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('@panwatch/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    // 预置图表实例: 强制 effect 走 setOption 分支(旧代码的 RangeError 就在这条路径上)
    chartRef: { current: { setOption: mocks.setOption, resize: vi.fn() } },
  }),
}))
vi.mock('@panwatch/biz-ui/lib/stock-colors', () => ({
  readStockColors: () => ({ up: '#ef4444', down: '#22c55e' }),
  withAlpha: (c: string) => c,
}))

import ForecastConeChart from '@/components/ForecastConeChart'

const baseProps = {
  symbol: '002361',
  lastClose: 10,
  lastDate: '2026-10-10',
  prediction: [10.1, 10.2],
  direction: 'up',
  p5: [9.9, 9.8],
  p95: [10.3, 10.4],
}

describe('ForecastConeChart 空 K 线边界(RangeError 回归)', () => {
  afterEach(() => cleanup())
  beforeEach(() => {
    mocks.fetchAPI.mockReset()
    mocks.setOption.mockReset()
  })

  it('空 K 线: 不抛 RangeError, 显式「无历史K线」空态', async () => {
    mocks.fetchAPI.mockResolvedValue({ klines: [] })
    render(<ForecastConeChart {...baseProps} />)

    // 空态上屏
    await waitFor(() => expect(screen.getByText('无历史K线')).toBeTruthy())
    // effect 确实跑过(hist=[] 时旧代码在此抛 RangeError); 修复后能安全走到 setOption
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    expect(document.querySelector('[data-chart-empty="1"]')).toBeTruthy()
  })

  it('单根 K 线: 不抛 RangeError, 正常渲染图表(非空态)', async () => {
    mocks.fetchAPI.mockResolvedValue({
      klines: [{ date: '2026-10-09', close: 9.9 }],
    })
    const { container } = render(<ForecastConeChart {...baseProps} />)

    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    expect(container.querySelector('.animate-pulse')).toBeNull()
    expect(screen.queryByText('无历史K线')).toBeNull()
  })
})
