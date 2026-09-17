/**
 * 设计稿 v2.1 §10.2① —— K 线周期 ↔ URL query 的双向映射。
 *
 * 设计稿规定的 URL 参数名与取值为: `?period=intra|m1|m5|m15|m30|m60|d1|w1|mn`
 * (默认 `d1`, 状态**必须写进 URL**, 刷新/分享链接不丢)。
 *
 * 与图表内部 `KlineInterval`(`1m|5m|15m|30m|60m|1d|1w|1mth`)不是同一套字面量,
 * 故单独收敛成一张表 —— 两处各写一份映射正是会漂移的地方。
 *
 * **`intra`(分时) 不在此表内**: 分时不是 K 线周期, 由 `MinuteLwcChart` 承担
 * (设计稿 §4.1 的"分时/日K/周K/月K"里, 前三者的切换器在 K 线大图顶部, 分时另属一张图)。
 * 解析到 `intra` 时返回 `undefined` → 调用方落回图表默认周期, **不假装支持**。
 */

import type { KlineInterval } from '@panwatch/biz-ui/components/KlineChart'

/** URL 取值 → 图表周期(缺项 = 该取值本图表不支持) */
const PERIOD_TO_INTERVAL: Readonly<Record<string, KlineInterval>> = {
  m1: '1m',
  m5: '5m',
  m15: '15m',
  m30: '30m',
  m60: '60m',
  d1: '1d',
  w1: '1w',
  mn: '1mth',
}

/** 图表周期 → URL 取值(反向表, 由正向表生成, 天然一一对应) */
const INTERVAL_TO_PERIOD: Readonly<Record<KlineInterval, string>> = Object.fromEntries(
  Object.entries(PERIOD_TO_INTERVAL).map(([period, interval]) => [interval, period]),
) as Record<KlineInterval, string>

/**
 * 解析 `?period=`。
 *
 * @returns 合法取值 → 对应周期; 非法/缺失/`intra` → `undefined`(调用方用图表默认周期)。
 *          绝不回退成"随便挑一个周期"——那会把用户的链接悄悄改语义。
 */
export function periodToInterval(raw: string | null | undefined): KlineInterval | undefined {
  if (typeof raw !== 'string') return undefined
  return PERIOD_TO_INTERVAL[raw.trim().toLowerCase()]
}

/** 周期 → `?period=` 取值。传入值恒在表内(联合类型), 故返回 string。 */
export function intervalToPeriod(i: KlineInterval): string {
  return INTERVAL_TO_PERIOD[i] ?? 'd1'
}

/** 该 `?period=` 取值是否为本图表支持的 K 线周期(供"链接被降级"时显式提示用)。 */
export function isSupportedPeriod(raw: string | null | undefined): boolean {
  return periodToInterval(raw) !== undefined
}
