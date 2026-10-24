// @vitest-environment jsdom
/**
 * Lane C 回归钉子（2026-10-24）
 *
 * 修的真 bug：模拟盘 0 收益时 `min == max`，5 个 Y 轴刻度全被格式化成「100.0万」。
 * 这里锁死「刻度至少能分出 2 个不同值」，并覆盖大数、单点、空数组与负数。
 */
import { describe, expect, it } from 'vitest'
import { computeEquityAxis } from '../../src/lib/equity-axis'

const labels = (values: number[]) => computeEquityAxis(values).ticks.map((t) => t.label)

describe('computeEquityAxis：Y 轴刻度不许退化', () => {
  it('0 收益（首尾相等、全序列同值）至少给出 2 个不同刻度', () => {
    const flat = [1000000, 1000000, 1000000, 1000000]
    const ls = labels(flat)
    expect(new Set(ls).size).toBeGreaterThanOrEqual(2)
    expect(ls.every((l) => l === '100.0万')).toBe(false)
  })

  it('定义域对称撑开，曲线不会贴着上下边', () => {
    const axis = computeEquityAxis([1000000, 1010000, 1005000])
    expect(axis.min).toBeLessThan(1000000)
    expect(axis.max).toBeGreaterThan(1010000)
  })

  it('正常波动时不会把范围撑得过大（pad 约为 1%）', () => {
    const axis = computeEquityAxis([1000000, 1020000])
    expect(axis.max - axis.min).toBeLessThan(200000)
  })

  it('大数（10 亿级）仍能分出层次', () => {
    const ls = labels([1_000_000_000, 1_000_000_000, 1_000_000_000])
    expect(new Set(ls).size).toBeGreaterThanOrEqual(2)
  })

  it('单点序列安全（不产生 NaN 刻度）', () => {
    const axis = computeEquityAxis([1234567])
    expect(axis.ticks.length).toBe(5)
    expect(axis.ticks.every((t) => Number.isFinite(t.value))).toBe(true)
  })

  it('空数组返回空刻度而不是抛错', () => {
    expect(computeEquityAxis([]).ticks).toEqual([])
  })

  it('负数定义域同样撑开', () => {
    const axis = computeEquityAxis([-100000, -100000])
    expect(axis.min).toBeLessThan(-100000)
    expect(axis.max).toBeGreaterThan(-100000)
  })
})
