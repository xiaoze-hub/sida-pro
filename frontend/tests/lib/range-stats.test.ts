// 2026-09-18 设计稿 v2.1 §10.2④ 区间统计 —— 纯函数口径。
//
// 这层是"拖拽选段 → 资金面板出区间统计"的计算真源(组件只渲染)。钉住四件事:
//  ① 区间内没有 K 线 → null(调用方清空该行, 不渲染空壳/不编造 0);
//  ② 首末价/涨跌幅/振幅按**区间内**的 K 线算, 区间外的 bar 不得混入;
//  ③ 明盘/暗盘累计只累非 null 值并给出"有值天数"; 整段无数据时累计必须是 null 而不是 0;
//  ④ 资金柱/事件按**日期**落区间(与 K 线同一日期集合), 不按时间戳近似。
import { describe, expect, it } from 'vitest'
import { computeRangeStats, type RangeBar } from '@panwatch/biz-ui/lib/range-stats'
import type { KlineEventPoint } from '@panwatch/biz-ui/klineEvents'

const DAY = 86400

/** 造一根日线: date='2026-09-0X', time=该日 00:00Z 秒 */
function bar(day: number, o: number, h: number, l: number, c: number): RangeBar {
  const date = `2026-09-${String(day).padStart(2, '0')}`
  return { time: Date.parse(`${date}T00:00:00Z`) / 1000, date, open: o, high: h, low: l, close: c }
}

const BARS: RangeBar[] = [
  bar(1, 10, 11, 9.5, 10.5),
  bar(2, 10.5, 12, 10.4, 11.8),
  bar(3, 11.8, 12.2, 11.0, 11.2),
  bar(4, 11.2, 11.5, 8.8, 9.0), // 区间低点 8.8
  bar(5, 9.0, 13.0, 9.0, 12.9), // 区间高点 13.0
]

const ev = (date: string, kind: string): KlineEventPoint =>
  ({ date, kind, label: kind, tone: 'neutral' }) as KlineEventPoint

describe('computeRangeStats', () => {
  it('区间内一根 K 线都没有 → null(不渲染空壳)', () => {
    const from = Date.parse('2026-08-01T00:00:00Z') / 1000
    const to = Date.parse('2026-08-05T00:00:00Z') / 1000
    expect(computeRangeStats(BARS, [], [], [], from, to)).toBeNull()
  })

  it('from/to 顺序颠倒也按同一区间算(取 min/max)', () => {
    const a = computeRangeStats(BARS, [], [], [], BARS[0].time, BARS[1].time)
    const b = computeRangeStats(BARS, [], [], [], BARS[1].time, BARS[0].time)
    expect(a).toEqual(b)
    expect(a?.bars).toBe(2)
  })

  it('首末价/涨跌幅/振幅只按区间内 K 线算', () => {
    // 只取 9/1~9/3 三段: 首收 10.5, 末收 11.2, 高 12, 低 9.5
    const s = computeRangeStats(BARS, [], [], [], BARS[0].time, BARS[2].time)
    expect(s).not.toBeNull()
    expect(s!.from).toBe('2026-09-01')
    expect(s!.to).toBe('2026-09-03')
    expect(s!.bars).toBe(3)
    expect(s!.firstClose).toBe(10.5)
    expect(s!.lastClose).toBe(11.2)
    expect(s!.changePct).toBeCloseTo(((11.2 - 10.5) / 10.5) * 100, 6)
    // 9/1~9/3: 首收 10.5, 末收 11.2, 高 12.2(9/3), 低 9.5(9/1)
    expect(s!.amplitudePct).toBeCloseTo(((12.2 - 9.5) / 9.5) * 100, 6)
  })

  it('全区间: 高点 13 / 低点 8.8 都要取到(不只看首末)', () => {
    const s = computeRangeStats(BARS, [], [], [], BARS[0].time, BARS[4].time)
    expect(s!.amplitudePct).toBeCloseTo(((13 - 8.8) / 8.8) * 100, 6)
    expect(s!.changePct).toBeCloseTo(((12.9 - 10.5) / 10.5) * 100, 6)
  })

  it('明盘/暗盘: 整段无数据 → 累计 null 且天数为 0(不是 0 元)', () => {
    const s = computeRangeStats(
      BARS,
      [{ date: '2026-09-01', ming_net: null, dark_net: null }],
      [],
      [],
      BARS[0].time,
      BARS[4].time,
    )
    expect(s!.mingNet).toBeNull()
    expect(s!.mingDays).toBe(0)
    expect(s!.darkNet).toBeNull()
    expect(s!.darkDays).toBe(0)
  })

  it('明盘/暗盘: 只累非 null 值 + 只算区间内日期(区间外不入账)', () => {
    const s = computeRangeStats(
      BARS,
      [
        { date: '2026-08-30', ming_net: 999, dark_net: 999 }, // 区间外, 必须被排除
        { date: '2026-09-01', ming_net: 100, dark_net: null },
        { date: '2026-09-02', ming_net: null, dark_net: -50 },
        { date: '2026-09-03', ming_net: 300, dark_net: 50 },
      ],
      [],
      [],
      BARS[0].time,
      BARS[2].time,
    )
    expect(s!.mingNet).toBe(400)
    expect(s!.mingDays).toBe(2)
    expect(s!.darkNet).toBe(0)
    expect(s!.darkDays).toBe(2)
  })

  it('open_net 旧别名仍被兜底(ming_net 缺失时读它)', () => {
    const s = computeRangeStats(
      BARS,
      [{ date: '2026-09-01', open_net: 77 }],
      [],
      [],
      BARS[0].time,
      BARS[0].time,
    )
    expect(s!.mingNet).toBe(77)
    expect(s!.mingDays).toBe(1)
  })

  it('字符串/NaN 脏值不当数字(不静默变 0)', () => {
    const s = computeRangeStats(
      BARS,
      [{ date: '2026-09-01', ming_net: 'abc' as unknown as number, dark_net: NaN }],
      [],
      [],
      BARS[0].time,
      BARS[0].time,
    )
    expect(s!.mingNet).toBeNull()
    expect(s!.darkNet).toBeNull()
  })

  it('事件按日期落区间并计数(区间外的不计)', () => {
    const s = computeRangeStats(
      BARS,
      [],
      [
        ev('2026-09-01', 'limit_up'),
        ev('2026-09-02', 'limit_up'),
        ev('2026-09-02', 'split_cluster'),
        ev('2026-08-20', 'limit_up'), // 区间外
      ],
      [],
      BARS[0].time,
      BARS[2].time,
    )
    expect(s!.eventCounts).toEqual({ limit_up: 2, split_cluster: 1 })
    expect(s!.eventTotal).toBe(3)
  })

  it('价位线: 只有价格落在区间高低之间的才纳进来', () => {
    const s = computeRangeStats(
      BARS,
      [],
      [],
      [
        { price: 10.0, kind: 'support' },   // 在 9.5~12 之间
        { price: 12.0, kind: 'pressure' },  // 边界, 含
        { price: 8.0, kind: 'support' },    // 区间下方 → 排除
        { price: 50, kind: 'pressure' },    // 区间上方 → 排除
      ],
      BARS[0].time,
      BARS[2].time,
    )
    expect(s!.priceLines.map((l) => l.price)).toEqual([10.0, 12.0])
  })

  it('空输入不抛(null 安全)', () => {
    expect(computeRangeStats([], null, null, null, 0, DAY)).toBeNull()
  })
})
