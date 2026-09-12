/** 连板梯队展示纯函数(v0.5.85, spec §5/§7): 日期格式化只在这一层做。 */

/** 紧凑 yyyymmdd → 2026-09-11; 非紧凑原样返回(不猜)。 */
export function fmtISODate(compact: string): string {
  return /^\d{8}$/.test(compact)
    ? `${compact.slice(0, 4)}-${compact.slice(4, 6)}-${compact.slice(6, 8)}`
    : compact
}

/** 1板=首板字样; 其余不标。 */
export function boardTag(boards: number): string | null {
  return boards === 1 ? '首板' : null
}

/** 断/炸组的昨板数文案; 无昨板数(今日首次触板)→「今日触板」。 */
export function prevBoardsLabel(prev: number | null | undefined): string {
  return prev == null ? '今日触板' : `昨${prev}板`
}

/** 横向梯队滚到最新一天(最右)。null 安全。 */
export function scrollToLatest(el: HTMLElement | null): void {
  if (el) el.scrollLeft = el.scrollWidth
}
