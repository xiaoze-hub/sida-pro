import { describe, expect, it } from 'vitest'
import type { StrategyParam } from '@panwatch/api'
import { activeOverrides, displayValue, parseParamInput } from '@/lib/strategy-params'

const p = (over: Partial<StrategyParam> = {}): StrategyParam => ({
  key: 'change_pct_max', label: '追高上限', value: 9.5, unit: '%',
  min: -10, max: 20, step: 0.5, ...over,
})

describe('displayValue', () => {
  it('未覆盖时显示策略原值', () => {
    expect(displayValue(p(), {})).toBe(9.5)
  })
  it('覆盖后显示覆盖值(包括改成 0 这种假值)', () => {
    expect(displayValue(p(), { change_pct_max: 0 })).toBe(0)
  })
})

describe('activeOverrides', () => {
  it('只发真正改过的键, 改回原值等于没改', () => {
    expect(activeOverrides([p()], { change_pct_max: 12 })).toEqual({ change_pct_max: 12 })
    expect(activeOverrides([p()], { change_pct_max: 9.5 })).toEqual({})
  })
  it('未声明的键与非有限值一律不发', () => {
    expect(activeOverrides([p()], { made_up: 3 })).toEqual({})
    expect(activeOverrides([p()], { change_pct_max: Number.NaN })).toEqual({})
    expect(activeOverrides(undefined, { change_pct_max: 3 })).toEqual({})
  })
})

describe('parseParamInput', () => {
  it('越界与非数字不覆盖(保留原值), 而不是静默夹到边界', () => {
    expect(parseParamInput('99', p())).toBeNull()
    expect(parseParamInput('-99', p())).toBeNull()
    expect(parseParamInput('', p())).toBeNull()
    expect(parseParamInput('abc', p())).toBeNull()
    expect(parseParamInput('11.5', p())).toBe(11.5)
  })
})
