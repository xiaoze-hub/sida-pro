// 生产崩溃回归: LWC v5 的 setMarkers 对"时间落在 K 线范围外"的 marker 会抛
// `Value is null` → 整页进错误边界。2026-09-12(周六)在 /quote/600519 实测到:
// 当天公告日期 2026-09-12, 而最后一根 K 线是 2026-09-11。
import { describe, expect, it } from 'vitest'
import { dayKey, filterMarkersInBarsRange } from '@panwatch/biz-ui/lib/chart-markers'

const bd = (y: number, m: number, d: number) => ({ year: y, month: m, day: d }) as never
/** 'YYYY-MM-DD' → 该日 00:00Z 的秒数(与组件里的 toChartTime 同一算法) */
const ts = (s: string) => Math.floor(new Date(s + 'T00:00:00Z').getTime() / 1000)

describe('dayKey', () => {
  it('BusinessDay 与同日 UTCTimestamp 归一到同一个序号', () => {
    expect(dayKey(bd(2026, 9, 11))).toBe(20260911)
    expect(dayKey(ts('2026-09-11') as never)).toBe(20260911)
  })
  it('坏值返回 null 而不是 NaN', () => {
    expect(dayKey(Number.NaN as never)).toBeNull()
    expect(dayKey('abc' as never)).toBeNull()
    expect(dayKey({ year: 2026, month: 9 } as never)).toBeNull()
  })
})

describe('filterMarkersInBarsRange', () => {
  const bars = [ts('2026-09-09'), ts('2026-09-10'), ts('2026-09-11')].map(t => t as never)

  it('范围内保留, 范围外(含"次日公告")丢弃', () => {
    const markers = [
      { time: ts('2026-09-10') as never, text: '内' },
      { time: ts('2026-09-12') as never, text: '周末当天的公告' },
      { time: ts('2026-09-01') as never, text: '太早' },
    ]
    expect(filterMarkersInBarsRange(markers, bars).map(m => m.text)).toEqual(['内'])
  })

  it('BusinessDay marker 与数字 bar 混用也能比(两种图表实现各处一种)', () => {
    const markers = [
      { time: bd(2026, 9, 11), text: '末根' },
      { time: bd(2026, 9, 12), text: '越界' },
    ]
    expect(filterMarkersInBarsRange(markers, bars).map(m => m.text)).toEqual(['末根'])
  })

  it('没有任何 K 线时全部丢弃(空图上画 marker 必崩)', () => {
    expect(filterMarkersInBarsRange([{ time: bd(2026, 9, 11) }], [])).toEqual([])
  })

  it('全为坏时间时不放开(宁可少画, 不崩页)', () => {
    const markers = [{ time: Number.NaN as never, text: 'x' }]
    expect(filterMarkersInBarsRange(markers, bars)).toEqual([])
  })

  it('空 marker 列表直接返回空, 不报错', () => {
    expect(filterMarkersInBarsRange([], bars)).toEqual([])
  })
})
