import type { Time } from 'lightweight-charts'

/**
 * 只保留最近 N 个图表标注（2026-10-24，L2-B）
 *
 * 审计实测：K 线上「红三兵 / 多方炮 / 单阳不破 / 弹簧 / 假突破」等形态标注会在一根柱附近
 * 叠 4~6 个，10px 文字互相压住，整段图不可读。产品要求「只显示最近 3 个」。
 *
 * 口径：按时间升序取**最后 N 个**（=最近的信号），返回仍保持升序 ——
 * lightweight-charts 的 markers 同样要求时间有序，不能直接 reverse。
 */
export function latestMarkers<T extends { time: Time }>(markers: T[], limit = 3): T[] {
  if (!Array.isArray(markers) || markers.length <= limit) return markers
  const timeOf = (m: T) => Number(m.time as unknown as number)
  const sorted = [...markers].sort((a, b) => timeOf(a) - timeOf(b))
  return sorted.slice(Math.max(0, sorted.length - limit))
}
