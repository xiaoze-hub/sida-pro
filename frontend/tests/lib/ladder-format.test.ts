import { describe, expect, it } from 'vitest'
import { boardTag, fmtISODate, fmtSignedAmount, prevBoardsLabel, scrollToLatest } from '@panwatch/biz-ui/lib/ladder-format'

/**
 * `fmtSignedAmount`(v0.6.0 遗留⑤ 从 `QuickRail` 局部实现收敛为共享):
 * 带1 `HeaderBand` 的「封单额」cell 与右栏「主力净额」行**必须同一套单位映射** ——
 * 同类金额在两个拥有面显示不同单位会被读成两个数。语义与原局部实现逐字相同。
 */
describe('fmtSignedAmount', () => {
  it('正值按 亿/万 分档(与 fmtAmount 同档, 无符号前缀)', () => {
    expect(fmtSignedAmount(812_000_000)).toBe('8.12亿')
    expect(fmtSignedAmount(23_000_000)).toBe('2300万')
    expect(fmtSignedAmount(9_999)).toBe('9999')
  })

  it('负值: 取绝对值分档后补 -(跌停封单 / 主力净流出)', () => {
    expect(fmtSignedAmount(-18_000_000)).toBe('-1800万')
    expect(fmtSignedAmount(-29575.36)).toBe('-3万')
    expect(fmtSignedAmount(-812_000_000)).toBe('-8.12亿')
  })

  it('0 是真值(未封板)而非缺值', () => expect(fmtSignedAmount(0)).toBe('0'))

  it('缺值/脏值 → --(不渲染 NaN万, 不当 0)', () => {
    expect(fmtSignedAmount(null)).toBe('--')
    expect(fmtSignedAmount(undefined)).toBe('--')
    expect(fmtSignedAmount('')).toBe('--')
    expect(fmtSignedAmount('   ')).toBe('--')
    expect(fmtSignedAmount('abc')).toBe('--')
    expect(fmtSignedAmount(NaN)).toBe('--')
    expect(fmtSignedAmount(Infinity)).toBe('--')
  })

  it('字符串数字(PG DECIMAL → JSON)照常分档', () => {
    expect(fmtSignedAmount('23000000')).toBe('2300万')
    expect(fmtSignedAmount('-18000000')).toBe('-1800万')
  })
})

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
    // 2026-09-26 起按日列视图最新在左 -> 滚到 0(原为 scrollWidth)
    expect(el.scrollLeft).toBe(0)
  })
  it('null 安全', () => expect(() => scrollToLatest(null)).not.toThrow())
})
