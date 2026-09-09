import { describe, expect, it } from 'vitest'

import { computeDrawdownSeries, maxDrawdownOf } from '../../src/lib/drawdown'

describe('computeDrawdownSeries (B5.1)', () => {
  it('每个新高点回撤为 0, 下跌点按峰值折算', () => {
    const series = computeDrawdownSeries([
      { date: '2026-09-01', equity: 100 },
      { date: '2026-09-02', equity: 120 },
      { date: '2026-09-03', equity: 90 },
      { date: '2026-09-04', equity: 110 },
    ])
    expect(series.map((p) => p.dd)).toEqual([0, 0, -25, -8.3333])
    expect(maxDrawdownOf(series)).toBe(-25)
  })

  it('跳过非法净值, 不产生 NaN 点', () => {
    const series = computeDrawdownSeries([
      { date: '2026-09-01', equity: 100 },
      { date: '2026-09-02', equity: Number.NaN },
      { date: '2026-09-03', equity: 50 },
    ])
    expect(series).toHaveLength(2)
    expect(series[1].dd).toBe(-50)
  })

  it('空输入安全返回', () => {
    expect(computeDrawdownSeries([])).toEqual([])
    expect(maxDrawdownOf([])).toBe(0)
  })
})
