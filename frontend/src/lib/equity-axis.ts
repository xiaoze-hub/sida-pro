/**
 * 收益曲线 Y 轴定义域（2026-10-24, Lane C）
 *
 * 修的是真 bug：原实现 `range = maxV - minV || 1`，0 收益时（minV == maxV == 1_000_000）
 * 5 个刻度按 0.25 递增，再被「除以 10000 保留 1 位小数」格式化后**全部变成「100.0万」**，
 * 轴看起来没坏但读者读不出任何差异。
 *
 * 规则：
 * 1. 定义域对称撑开：`值 ± max(|值| × 1%, 1000)`，不为 0 收益画出一条假的陡坡；
 * 2. 刻度格式化后**去重**，不足 2 个不同值就按 10 倍继续撑，直到能分出层次（最多 6 次）；
 * 3. 返回值同时给「值」与「显示串」，组件不再自己格式化，避免两处口径漂移。
 */
import { safeFixed } from '@/lib/format'

export interface EquityAxisTick {
  value: number
  label: string
}

export interface EquityAxis {
  min: number
  max: number
  ticks: EquityAxisTick[]
}

// 走 @/lib/format 的 safe*（R6 铁律：新代码禁裸 .toFixed — PG DECIMAL 到前端是字符串）
const defaultFormat = (v: number): string => `${safeFixed(v / 10000, 1)}万`

export function computeEquityAxis(
  values: number[],
  yTicks = 4,
  format: (v: number) => string = defaultFormat,
): EquityAxis {
  const finite = values.filter((v) => Number.isFinite(v))
  if (finite.length === 0) return { min: 0, max: 0, ticks: [] }

  const rawMin = Math.min(...finite)
  const rawMax = Math.max(...finite)
  const base = Math.max(Math.abs(rawMin), Math.abs(rawMax), 1)

  let min = rawMin
  let max = rawMax
  let ticks: EquityAxisTick[] = []
  let padV = Math.max(base * 0.01, 1000)

  for (let attempt = 0; attempt < 6; attempt++) {
    const lo = rawMin - padV
    const hi = rawMax + padV
    const step = (hi - lo) / yTicks
    ticks = Array.from({ length: yTicks + 1 }, (_, i) => {
      const value = lo + step * i
      return { value, label: format(value) }
    })
    min = lo
    max = hi
    if (new Set(ticks.map((t) => t.label)).size >= 2) break
    padV *= 10
  }

  return { min, max, ticks }
}
