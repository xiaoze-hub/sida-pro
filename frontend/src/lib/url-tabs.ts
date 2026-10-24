/**
 * URL ?tab= 深层链接解析(2026-10-24 小白可用性)。
 *
 * 背景: 机会页工具页签状态只存 localStorage, 页外入口(首页共振面板「全部 ›」、
 * 个股工作台铁三角结论等)无法直接带到目标页签 —— 用户点过去还要自己再找一次,
 * 小白用户经常在这一步流失。统一走 `/opportunities?tab=<value>` 直达。
 *
 * 纯函数, 不碰 window.location —— 便于单测; 调用方传 location.search。
 */

/** 从 search 串解析合法的 tab 值; 非法/缺失返回 null(调用方保持默认不动)。 */
export function tabFromSearch(search: string, allowed: readonly string[]): string | null {
  if (!search) return null
  const raw = search.startsWith('?') ? search.slice(1) : search
  if (!raw) return null
  const value = new URLSearchParams(raw).get('tab')
  if (!value) return null
  return allowed.includes(value) ? value : null
}
