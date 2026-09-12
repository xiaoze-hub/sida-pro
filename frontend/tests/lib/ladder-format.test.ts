import { describe, expect, it } from 'vitest'
import { boardTag, fmtISODate, prevBoardsLabel, scrollToLatest } from '@panwatch/biz-ui/lib/ladder-format'

describe('fmtISODate', () => {
  it('紧凑日期转全格式', () => expect(fmtISODate('20260911')).toBe('2026-09-11'))
  it('非紧凑原样返回(不猜)', () => expect(fmtISODate('2026-09-11')).toBe('2026-09-11'))
})

describe('boardTag', () => {
  it('1板=首板, 其余 null', () => {
    expect(boardTag(1)).toBe('首板')
    expect(boardTag(3)).toBeNull()
  })
})

describe('prevBoardsLabel', () => {
  it('有昨板数/无昨板数', () => {
    expect(prevBoardsLabel(3)).toBe('昨3板')
    expect(prevBoardsLabel(null)).toBe('今日触板')
    expect(prevBoardsLabel(undefined)).toBe('今日触板')
  })
})

describe('scrollToLatest', () => {
  it('滚到最右', () => {
    const el = { scrollWidth: 900, scrollLeft: 0 } as unknown as HTMLElement
    scrollToLatest(el)
    expect(el.scrollLeft).toBe(900)
  })
  it('null 安全', () => expect(() => scrollToLatest(null)).not.toThrow())
})
