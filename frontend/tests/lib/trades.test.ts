import { describe, expect, it } from 'vitest'

import { computeRealizedPnlSeries, realizedByDate } from '../../src/lib/trades'

describe('computeRealizedPnlSeries (B5.2)', () => {
  it('按平仓日累计, 同日多笔合并', () => {
    const series = computeRealizedPnlSeries([
      { exit_date: '2026-09-02', pnl: 100 },
      { exit_date: '2026-09-01', pnl: -50 },
      { exit_date: '2026-09-02', pnl: 30 },
    ])
    expect(series.map((p) => p.date)).toEqual(['2026-09-01', '2026-09-02'])
    expect(series.map((p) => p.pnl)).toEqual([-50, 80])
  })

  it('跳过缺失/非法盈亏, 不产生 NaN', () => {
    const series = computeRealizedPnlSeries([
      { exit_date: '', pnl: 10 },
      { exit_date: '2026-09-03', pnl: Number.NaN },
      { exit_date: '2026-09-04', pnl: 20 },
    ])
    expect(series).toEqual([{ date: '2026-09-04', pnl: 20 }])
  })

  it('空输入与 null 安全', () => {
    expect(computeRealizedPnlSeries([])).toEqual([])
    expect(realizedByDate([{ exit_date: '2026-09-05', pnl: 5 }])).toEqual([
      { date: '2026-09-05', pnl: 5 },
    ])
  })
})
