/**
 * 涨跌色设计令牌读取 — 全站统一收敛到 index.css 的 --stock-up / --stock-down。
 *
 * 红涨绿跌 (A股口径): --stock-up 红 = 涨/资金流入, --stock-down 绿 = 跌/资金流出。
 *
 * Tailwind 场景直接用工具类 (text-stock-up / text-stock-down / bg-stock-up ...)。
 * 图表库 (ECharts / LightweightCharts) 无法消费 Tailwind 类, 用本模块在运行时
 * 读取 CSS 变量, 保证与工具类同源、随 light/dark 自动切换。
 */

/** 从 :root 读取 CSS 变量, 失败/SSR 时回退到令牌默认值。 */
export function cssVar(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
  } catch {
    return fallback
  }
}

export interface StockColors {
  /** 涨 = 红 (--stock-up) */
  up: string
  /** 跌 = 绿 (--stock-down) */
  down: string
}

/** 读取涨跌色令牌。图表 JS 统一入口, 禁止再硬编码 #ef4444/#22c55e 等。 */
export function readStockColors(): StockColors {
  return {
    up: cssVar('--stock-up', '#E53935'),
    down: cssVar('--stock-down', '#43A047'),
  }
}

/** hex → rgba(alpha)。输入非 6 位 hex 时原样返回 (兜底不抛错)。 */
export function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(color)
  if (!m) return color
  const n = parseInt(m[1], 16)
  const r = (n >> 16) & 255
  const g = (n >> 8) & 255
  const b = n & 255
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}
