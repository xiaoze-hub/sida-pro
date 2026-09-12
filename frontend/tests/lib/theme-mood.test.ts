import { describe, expect, it } from 'vitest'
import {
  axisDates,
  axisWidth,
  cellColorClass,
  cellTextClass,
  cellsByDate,
  columnCenterX,
  dayLabels,
  fmtScore,
  monthBands,
  scoreTrend,
} from '@/lib/theme-mood'

describe('fmtScore', () => {
  it('保留一位小数, 空值占位', () => {
    expect(fmtScore(78.24)).toBe('78.2')
    expect(fmtScore(null)).toBe('--')
  })
})

describe('cellColorClass', () => {
  it('低分灰/中性淡/高分渐强', () => {
    expect(cellColorClass(null)).toBe('bg-muted/30')
    expect(cellColorClass(45)).toBe('bg-muted/40')
    expect(cellColorClass(62)).toBe('bg-stock-up/15')
    expect(cellColorClass(78)).toBe('bg-stock-up/30')
    expect(cellColorClass(90)).toBe('bg-stock-up/45')
  })
})

describe('cellTextClass', () => {
  it('≥60 用涨色, 其余弱化', () => {
    expect(cellTextClass(70)).toContain('text-stock-up')
    expect(cellTextClass(50)).toContain('text-muted-foreground')
  })
})

describe('monthBands', () => {
  it('按自然月分组并统计交易日数(供月份带宽度)', () => {
    expect(monthBands(['20260831', '20260901', '20260902', '20260903'])).toEqual([
      { key: '202608', label: '8月', count: 1 },
      { key: '202609', label: '9月', count: 3 },
    ])
    expect(monthBands([])).toEqual([])
  })
})

describe('dayLabels', () => {
  it('首个交易日与跨月首日显示 M/D, 其余只显示日号', () => {
    expect(dayLabels(['20260831', '20260901', '20260902', '20260903'])).toEqual(['8/31', '9/1', '2', '3'])
  })
})

describe('axisDates', () => {
  const cells = [
    { date: '20260901', score: 61.0, limit_up_cnt: 2 },
    { date: '20260902', score: 66.0, limit_up_cnt: 3 },
  ]
  it('优先共享轴, 缺省回退首个题材 cells, 并按窗口截断', () => {
    expect(axisDates(['20260901', '20260902', '20260903'], cells, 2)).toEqual(['20260902', '20260903'])
    expect(axisDates(undefined, cells, 20)).toEqual(['20260901', '20260902'])
    expect(axisDates([], cells, 20)).toEqual(['20260901', '20260902'])
    expect(axisDates(undefined, [], 20)).toEqual([])
  })
})

describe('cellsByDate', () => {
  it('按日期建索引, 缺该交易日返回 undefined', () => {
    const m = cellsByDate([{ date: '20260901', score: 61.0, limit_up_cnt: 2 }])
    expect(m.get('20260901')?.score).toBe(61.0)
    expect(m.get('20260902')).toBeUndefined()
  })
})

describe('轴几何', () => {
  it('轴宽与列中心跟色块行逐像素一致(38 格 + 2 隙)', () => {
    expect(axisWidth(0)).toBe(0)
    expect(axisWidth(1)).toBe(38)
    expect(axisWidth(4)).toBe(4 * 38 + 3 * 2)
    expect(columnCenterX(0)).toBe(19)
    expect(columnCenterX(3)).toBe(3 * 40 + 19)
  })
})

describe('scoreTrend', () => {
  it('按列中心取点, 与时间轴逐列对齐', () => {
    const t = scoreTrend([50, 60, 70], { height: 40 })!
    expect(t.dots.map((d) => d.x)).toEqual([19, 59, 99])
    expect(t.lines).toHaveLength(1)
    expect(t.areas).toHaveLength(1)
    expect(t.last?.score).toBe(70)
    expect([t.lo, t.hi]).toEqual([50, 70])
  })

  it('空序列或全空值 → null', () => {
    expect(scoreTrend([], { height: 40 })).toBeNull()
    expect(scoreTrend([null, undefined], { height: 40 })).toBeNull()
  })

  it('缺交易日断线, 不跨缺口连成假直线', () => {
    const t = scoreTrend([50, null, 70], { height: 40 })!
    expect(t.lines).toHaveLength(2)
    expect(t.dots.map((d) => d.i)).toEqual([0, 2])
  })

  it('平稳段有保底跨度, 不被放大成剧烈波动', () => {
    const flat = scoreTrend([50, 51], { height: 40, minSpan: 8 })!
    const steep = scoreTrend([20, 90], { height: 40, minSpan: 8 })!
    const dy = (t: typeof flat) => Math.abs(t.dots[0].y - t.dots[1].y)
    expect(dy(flat)).toBeLessThan(8)
    expect(dy(steep)).toBeGreaterThan(20)
  })

  it('点落在绘图区内(padY 内不收越界)', () => {
    const t = scoreTrend([10, 55, 99], { height: 40, padY: 6 })!
    for (const d of t.dots) {
      expect(d.y).toBeGreaterThanOrEqual(6)
      expect(d.y).toBeLessThanOrEqual(34)
    }
  })
})
