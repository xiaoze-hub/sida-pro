import { describe, expect, it } from 'vitest'
import {
  SUBCHARTS,
  subChartOf,
  subChartReadouts,
  visibleSubCharts,
  type SubChartRow,
} from '@panwatch/biz-ui/lib/subcharts'

const row = (over: Partial<SubChartRow> = {}): SubChartRow => ({
  date: '2026-09-11', volume: 123456, volMa5: 100000, volMa10: 90000,
  macd: 0.1234, signal: -0.05, hist: 0.3668, rsi6: 71.2, ...over,
})

describe('SUBCHARTS 注册表', () => {
  it('pane 顺序唯一且信息栏按 pane 排', () => {
    expect(SUBCHARTS.map(s => s.paneIndex)).toEqual([1, 2, 3])
    expect(new Set(SUBCHARTS.map(s => s.key)).size).toBe(SUBCHARTS.length)
    expect(visibleSubCharts().map(s => s.key)).toEqual(['vol', 'macd', 'rsi'])
  })
  it('标题带参数口径(读数必须可复核)', () => {
    expect(subChartOf('macd').label).toBe('MACD(12,26,9)')
    expect(subChartOf('rsi').label).toBe('RSI(6)')
  })
  it('关掉 RSI 后信息栏不再挂它', () => {
    expect(visibleSubCharts({ rsi: true }).map(s => s.key)).toEqual(['vol', 'macd'])
  })
})

describe('subChartReadouts', () => {
  it('成交量换算成万手并保留均量', () => {
    const r = subChartReadouts(subChartOf('vol'), row())
    expect(r.map(x => x.label)).toEqual(['量', 'MA5', 'MA10'])
    expect(r[0].value).toBe('12.3万手')
  })
  it('MACD 柱按方向给 tone(只有价格类读数才允许红绿)', () => {
    const up = subChartReadouts(subChartOf('macd'), row({ hist: 0.3668 })).find(x => x.label === '柱')
    const down = subChartReadouts(subChartOf('macd'), row({ hist: -0.2 })).find(x => x.label === '柱')
    expect(up?.tone).toBe('up')
    expect(down?.tone).toBe('down')
    expect(subChartOf('macd').readouts(row({ hist: null })).find(x => x.label === '柱')?.value).toBe('--')
  })
  it('RSI 分位区: 70 以上超买 / 30 以下超卖 / 其余中性', () => {
    const zone = (v: number | null) =>
      subChartOf('rsi').readouts(row({ rsi6: v })).find(x => x.label === '区')?.value
    expect(zone(71.2)).toBe('超买')
    expect(zone(28)).toBe('超卖')
    expect(zone(50)).toBe('中性')
    expect(zone(null)).toBe('--')
  })
  it('没有数据行时整栏读 "--", 不补 0', () => {
    expect(subChartReadouts(subChartOf('vol'), null).every(x => x.value === '--')).toBe(true)
    expect(subChartReadouts(subChartOf('vol'), undefined as unknown as SubChartRow)
      .every(x => x.value === '--')).toBe(true)
  })
})
