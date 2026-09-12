/** Marker 时间裁剪(v0.5.79): 防 lightweight-charts v5 因"时间不在数据范围内"整页崩。
 *
 * 现象(2026-09-12 周六生产实测): 行情页 `/quote/600519` 直接进错误边界
 * `Error: Value is null`, 栈停在 `setMarkers`。原因: 当天的公告事件日期 = 2026-09-12,
 * 而 K 线最后一根 = 2026-09-11 —— **非交易日(周末/节假日)当天有事件但没有那根 K 线**,
 * marker 时间落在 series 范围外, LWC v5 转换时拿到 null 就抛。
 * 同类问题此前已用"过滤 OHLC 为 null 的 K 线"治过一版(见 KlineChart.load), 这是它的另一半。
 *
 * 只裁范围, 不猜位置: 范围外的 marker 直接不画(事件本身仍在事件列表里可见),
 * 不做"贴到最后一根"的假定位。
 */
import type { Time } from 'lightweight-charts'

type DayParts = { year: number; month: number; day: number }

/** 统一成可比较的日序号: BusinessDay 直接用, UTCTimestamp(秒) 按 UTC 取日。 */
export function dayKey(t: Time): number | null {
  if (typeof t === 'number' || typeof t === 'string') {
    const sec = typeof t === 'number' ? t : Math.floor(Number(t))
    if (!Number.isFinite(sec)) return null
    const d = new Date(sec * 1000)
    if (Number.isNaN(d.getTime())) return null
    return d.getUTCFullYear() * 10000 + (d.getUTCMonth() + 1) * 100 + d.getUTCDate()
  }
  const p = t as DayParts
  if (!p || !Number.isFinite(p.year) || !Number.isFinite(p.month) || !Number.isFinite(p.day)) return null
  return p.year * 10000 + p.month * 100 + p.day
}

/** 只保留时间落在首/末根 K 线之间(含)的 marker; 没有 K 线时全丢(画在空图上必崩)。 */
export function filterMarkersInBarsRange<T extends { time: Time }>(markers: T[], barTimes: Time[]): T[] {
  if (!barTimes.length) return markers.length ? [] : []
  let lo = Infinity
  let hi = -Infinity
  for (const t of barTimes) {
    const k = dayKey(t)
    if (k === null) continue
    if (k < lo) lo = k
    if (k > hi) hi = k
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return []
  return markers.filter((m) => {
    const k = dayKey(m.time)
    return k !== null && k >= lo && k <= hi
  })
}
