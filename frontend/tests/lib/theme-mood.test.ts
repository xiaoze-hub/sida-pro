import { describe, expect, it } from 'vitest'
import { cellColorClass, cellTextClass, fmtScore, splitWindows } from '@/lib/theme-mood'

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

describe('splitWindows', () => {
  it('把矩阵行按窗口截断到最近 N 天', () => {
    const cells = Array.from({ length: 30 }, (_, i) => ({ date: `202609${String(i + 1).padStart(2, '0')}`, score: i * 3 }))
    expect(splitWindows(cells, 20)).toHaveLength(20)
    expect(splitWindows(cells, 10)[0].date).toBe('20260921')
  })
})
