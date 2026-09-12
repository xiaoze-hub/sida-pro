import { describe, expect, it } from 'vitest'
import {
  axisDates,
  cellColorClass,
  cellTextClass,
  cellsByDate,
  dayLabels,
  fmtScore,
  monthBands,
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
