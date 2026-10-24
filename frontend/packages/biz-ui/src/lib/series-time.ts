/**
 * lightweight-charts 序列时间收敛（2026-10-24，P0）
 *
 * LWC 对 `setData` 的约束是**硬断言**：时间必须为正、严格升序、不得重复。
 * 违反即抛 `Assertion failed: data must be asc ordered by time`，经 ErrorBoundary
 * 放大成**整页崩溃**（实测 `/stocks/600519` 全页白屏）。
 *
 * 触发根因（查实）：`fund_flow` 由后端 bars 1:1 生成，可能含**同日重复行**；
 * 图表层此前只 sort 不去重 → 相邻两条同 time → 断言命中（index=117，两次 1792368000）。
 *
 * 结论：任何来自接口/props 的时间序列都不能假设有序，统一走这里收敛。
 * 注意：**去重是按 time**，不是按日期字符串 —— 周/月线分桶后不同日期可能落到同一 time。
 */
import type { Time } from 'lightweight-charts'

export interface TimePoint {
  time: Time
  [k: string]: unknown
}

export interface AscendResult<T> {
  points: T[]
  dropped: number
}

/** 数值化 time（BusinessDay 对象等非数值形态 → NaN，会被过滤掉） */
export function timeOf(point: { time: Time }): number {
  return Number(point.time as unknown as number)
}

/**
 * 过滤非法时间 → 升序 → 按 time 去重。
 * @param tag 序列名，仅用于 warn 日志（便于回到数据源而不是靠图表崩）
 */
export function ascendSeries<T extends { time: Time }>(points: T[], tag = 'series'): T[] {
  const clean = points.filter((p) => {
    const t = timeOf(p)
    return Number.isFinite(t) && t > 0
  })
  clean.sort((a, b) => timeOf(a) - timeOf(b))
  const dedup = clean.filter((p, i) => i === 0 || timeOf(p) !== timeOf(clean[i - 1]))
  if (dedup.length !== points.length && typeof console !== 'undefined') {
    console.warn(`[KlineChart] ${tag} 时间乱序/重复/非法, 已收敛`, { raw: points.length, kept: dedup.length })
  }
  return dedup
}

/** 同 ascendSeries，但额外返回被丢弃的条数（供测试/巡检断言） */
export function ascendSeriesWithStats<T extends { time: Time }>(points: T[], tag = 'series'): AscendResult<T> {
  const after = ascendSeries(points, tag)
  return { points: after, dropped: points.length - after.length }
}
