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

/** HSL 通道变量 → hsla() 字符串 (index.css 里 --background/--border 等都是 "H S% L%" 通道格式)。 */
export function hslaVar(name: string, fallback: string, alpha = 1): string {
  const ch = cssVar(name, fallback)
  return alpha >= 1 ? `hsl(${ch})` : `hsla(${ch}, ${alpha})`
}

export interface ChartTheme {
  /** 图表面底 (跟随 light/dark, 原硬编码 #0f172a 只对 dark 友好) */
  bg: string
  /** 轴文字 */
  text: string
  /** 网格 hairline */
  grid: string
  /** 轴边框 */
  border: string
  /** 十字光标线 */
  crosshair: string
  /** 光标标签底 */
  labelBg: string
  /** 中性 marker (原 #64748b) */
  neutral: string
  /** 无数据柱 (原 #475569) */
  nodata: string
}

/** Lightweight Charts 主题跟随: 全从 CSS 变量读, light/dark 自动切换, 禁止硬编码 slate。 */
export function readChartTheme(): ChartTheme {
  return {
    bg: hslaVar('--background', '240 20% 95.5%'),
    text: hslaVar('--muted-foreground', '240 10% 48%'),
    grid: hslaVar('--border', '240 15% 90%'),
    border: hslaVar('--border', '240 15% 90%'),
    crosshair: hslaVar('--muted-foreground', '240 10% 48%'),
    labelBg: hslaVar('--card', '240 25% 99%'),
    neutral: hslaVar('--muted-foreground', '240 10% 48%'),
    nodata: hslaVar('--muted-foreground', '240 10% 48%', 0.45),
  }
}

/** L1 均线灰阶: muted 基, 透明度分档 (原 rgba slate 硬编码)。牛/马/MACD 蓝橙为专业图表语义色, 保留。 */
export function maShade(alpha: number): string {
  return hslaVar('--muted-foreground', '240 10% 48%', alpha)
}
