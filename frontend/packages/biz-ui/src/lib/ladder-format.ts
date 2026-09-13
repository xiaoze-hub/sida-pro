/** 连板梯队展示纯函数(v0.5.85, spec §5/§7): 日期格式化只在这一层做。 */

/** 手动 2 位小数(不用 toFixed, UI 规则 R6)。 */
function fixed2(v: number): string {
  const cents = Math.round(Math.abs(v) * 100)
  const int = Math.floor(cents / 100)
  const frac = cents % 100
  return `${int}.${String(frac).padStart(2, '0')}`
}

/** 带符号百分比: +10.02% / -4.81%; null → '--'。 */
export function fmtPct(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '--'
  const sign = p > 0 ? '+' : p < 0 ? '-' : ''
  return `${sign}${fixed2(p)}%`
}

/** 成交额: >=1亿 → x.xx亿; >=1万 → x万; null → '--'。 */
export function fmtAmount(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '--'
  if (v >= 1e8) return `${fixed2(v / 1e8)}亿`
  if (v >= 1e4) return `${Math.round(v / 1e4)}万`
  return String(Math.round(v))
}

/**
 * **带符号**金额(元 → 万/亿): 用于可为负的资金读数 —— 封单额 `FCAmo < 0` = 跌停封单、
 * 主力净额 `Zjl_HB < 0` = 净流出。`fmtAmount` 只对正值分档(负值会原样吐出 `-18000000`),
 * 故这里取绝对值分档后补 `-`; `0` 是真值(未封板)⇒ 渲染 `0` 而不是 `--`。
 *
 * 单一实现(v0.6.0 遗留⑤): 带1 `HeaderBand`(封单额)与右栏 `QuickRail`(主力净额)**共用** ——
 * 同一类金额在不同拥有面必须同一套单位映射, 否则会被读成两个数。
 * 入参 `unknown`: PG DECIMAL 经 JSON 到前端可能是字符串(`"8.12E+8"`/`"23000000"`),
 * 一律先安全转数值 —— 空串/非数字/NaN/Infinity → `--`(不渲染 `NaN万`, 不当 0)。
 */
export function fmtSignedAmount(v: unknown): string {
  if (v == null) return '--'
  const s = typeof v === 'string' ? v.trim() : v
  if (s === '') return '--'
  const n = typeof s === 'number' ? s : Number(s)
  if (!Number.isFinite(n)) return '--'
  return `${n < 0 ? '-' : ''}${fmtAmount(Math.abs(n))}`
}

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
