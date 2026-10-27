/**
 * P0 回归钉子（2026-10-24）：K 线序列时间收敛
 *
 * 事故：`/stocks/600519` 整页崩，报
 * `Assertion failed: data must be asc ordered by time, index=117, time=1792368000, prev time=1792368000`
 * 根因：`fund_flow` 由后端 bars 1:1 生成、含**同日重复行**；图表层只 sort 不去重，
 * 相邻两条同 time 直接触发 lightweight-charts 的硬断言。
 *
 * 这里用真实事故数值做 fixture，锁死「重复 / 乱序 / 负值 / NaN / 非数值 time」五种脏数据。
 */
import { describe, expect, it, vi } from 'vitest'
import { ascendSeries, ascendSeriesWithStats, timeOf } from '@sida/biz-ui/lib/series-time'

const p = (time: number, extra: Record<string, unknown> = {}) => ({ time: time as never, ...extra })

describe('ascendSeries：重复时间（本次事故）', () => {
  it('相邻同 time 必须去重，只保留一条', () => {
    const raw = [p(1792368000), p(1792368000), p(1792627200)]
    const out = ascendSeries(raw, 'fundFlow')
    expect(out).toHaveLength(2)
    expect(out.map(timeOf)).toEqual([1792368000, 1792627200])
  })

  it('事故复现：index=117 两条 1792368000 不得同时存在', () => {
    const raw = Array.from({ length: 118 }, (_, i) => p(1790000000 + i * 86400))
    raw.push(p(1792368000), p(1792368000)) // 同日重复（fund_flow 两行）
    const out = ascendSeries(raw, 'fundFlow')
    const times = out.map(timeOf)
    expect(new Set(times).size).toBe(times.length)
    expect(times).toEqual([...times].sort((a, b) => a - b))
  })
})

describe('ascendSeries：乱序与非法值', () => {
  it('乱序会被排成升序', () => {
    const out = ascendSeries([p(300), p(100), p(200)], 'candles')
    expect(out.map(timeOf)).toEqual([100, 200, 300])
  })

  it('负值 / 0 / NaN / Infinity 全部剔除', () => {
    const out = ascendSeries([p(-1792368000), p(0), p(Number.NaN), p(Number.POSITIVE_INFINITY), p(100)], 'candles')
    expect(out.map(timeOf)).toEqual([100])
  })

  it('保留额外字段（value/color 不被破坏）', () => {
    const out = ascendSeries([{ time: 200 as never, value: 2 }, { time: 100 as never, value: 1 }], 'volume')
    expect(out[0]).toMatchObject({ value: 1 })
    expect(out[1]).toMatchObject({ value: 2 })
  })

  it('已合法序列不产生 warn（避免噪声）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    ascendSeries([p(100), p(200), p(300)], 'ok')
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('有丢弃（重复/非法）时 warn 一次并带 tag', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    ascendSeries([p(100), p(100)], 'fundFlow')
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('fundFlow')
    warn.mockRestore()
  })

  it('只是乱序（无丢弃）不 warn —— 排序不是数据丢失', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    ascendSeries([p(200), p(100)], 'fundFlow')
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })
})

describe('ascendSeriesWithStats', () => {
  it('返回丢弃条数，供巡检断言', () => {
    const { points, dropped } = ascendSeriesWithStats([p(100), p(100), p(-1)], 'x')
    expect(points.map(timeOf)).toEqual([100])
    expect(dropped).toBe(2)
  })
})
