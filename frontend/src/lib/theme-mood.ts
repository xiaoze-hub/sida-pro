/** 题材情绪分纯函数(2026-09-12): 色阶/格式化/窗口切分, 供页面与测试复用。 */
import { safeFixed } from '@/lib/format'

export interface MoodCell {
  date: string
  score: number | null
  limit_up_cnt: number | null
}

export function fmtScore(v: number | null | undefined): string {
  if (v == null) return '--'
  return safeFixed(v, 1)
}

export function cellColorClass(score: number | null | undefined): string {
  if (score == null || !Number.isFinite(score)) return 'bg-muted/30'
  if (score < 50) return 'bg-muted/40'
  if (score < 60) return 'bg-muted/60'
  if (score < 70) return 'bg-stock-up/15'
  if (score < 82) return 'bg-stock-up/30'
  return 'bg-stock-up/45'
}

export function cellTextClass(score: number | null | undefined): string {
  if (score != null && Number.isFinite(score) && score >= 60) return 'text-stock-up font-medium'
  return 'text-muted-foreground'
}

/** 矩阵时间轴几何: 单元格 38px + 间隙 2px(与页面 gap-0.5 一致)。 */
export const AXIS_CELL_W = 38
export const AXIS_PITCH = 40
export const AXIS_GAP = AXIS_PITCH - AXIS_CELL_W

/** 轴总宽(与矩阵色块行逐像素一致, 供曲线 SVG 复用同一列几何)。 */
export function axisWidth(count: number): number {
  return count > 0 ? count * AXIS_CELL_W + (count - 1) * AXIS_GAP : 0
}

/** 第 i 列的列中心 x(色块宽 38, 曲线取点即落在此处, 保证与时间轴逐列对齐)。 */
export function columnCenterX(i: number): number {
  return i * AXIS_PITCH + AXIS_CELL_W / 2
}

export interface TrendDot {
  i: number
  x: number
  y: number
  score: number
}

export interface TrendLine {
  lines: string[]
  areas: string[]
  dots: TrendDot[]
  last: TrendDot | null
  lo: number
  hi: number
}

/**
 * 情绪走势折线: 按轴列几何取点。纵轴自适应用数据区间但保底 minSpan(默认 8 分),
 * 避免平稳段被放大成剧烈波动; lo/hi 回传原始极值供界面标注, 让缩放是"看得见的"。
 * 序列中间缺交易日(如题材当日未入池)时断线, 不跨缺口连成假直线。
 */
export function scoreTrend(
  values: (number | null | undefined)[],
  opts: { height: number; padY?: number; minSpan?: number },
): TrendLine | null {
  const padY = opts.padY ?? 6
  const inner = Math.max(opts.height - padY * 2, 1)
  const pts: { i: number; score: number }[] = []
  values.forEach((v, i) => {
    if (v != null && Number.isFinite(v)) pts.push({ i, score: Number(v) })
  })
  if (!pts.length) return null

  const lo = Math.min(...pts.map((p) => p.score))
  const hi = Math.max(...pts.map((p) => p.score))
  const span = Math.max(hi - lo, opts.minSpan ?? 8)
  const mid = (lo + hi) / 2
  const base = mid - span / 2
  const yOf = (s: number) => Math.round((padY + (1 - (s - base) / span) * inner) * 10) / 10

  const segments: TrendDot[][] = []
  let cur: TrendDot[] = []
  pts.forEach((p, idx) => {
    if (idx > 0 && p.i !== pts[idx - 1].i + 1 && cur.length) {
      segments.push(cur)
      cur = []
    }
    cur.push({ i: p.i, x: columnCenterX(p.i), y: yOf(p.score), score: p.score })
  })
  if (cur.length) segments.push(cur)

  return {
    lines: segments.map((seg) => seg.map((p) => `${p.x},${p.y}`).join(' ')),
    areas: segments.map(
      (seg) =>
        `M ${seg[0].x},${opts.height} L ${seg.map((p) => `${p.x},${p.y}`).join(' L ')}` +
        ` L ${seg[seg.length - 1].x},${opts.height} Z`,
    ),
    dots: segments.flat(),
    last: segments.length ? segments[segments.length - 1].slice(-1)[0] : null,
    lo,
    hi,
  }
}

export interface MonthBand {
  key: string
  label: string
  count: number
}

/** 月份带: 连续交易日按自然月分组, 供矩阵表头月份标注行(宽度 = count × pitch - gap)。 */
export function monthBands(dates: string[]): MonthBand[] {
  const out: MonthBand[] = []
  for (const d of dates) {
    const key = d.slice(0, 6)
    const last = out[out.length - 1]
    if (last && last.key === key) last.count += 1
    else out.push({ key, label: `${Number(d.slice(4, 6))}月`, count: 1 })
  }
  return out
}

/** 日号标注: 首个交易日与跨月首日显示 M/D, 其余显示 dd。 */
export function dayLabels(dates: string[]): string[] {
  return dates.map((d, i) => {
    const day = Number(d.slice(6, 8))
    const prev = i > 0 ? dates[i - 1] : null
    if (!prev || prev.slice(4, 6) !== d.slice(4, 6)) return `${Number(d.slice(4, 6))}/${day}`
    return String(day)
  })
}

/** 共享时间轴: 优先接口 dates, 缺省回退首个题材的 cells(兼容旧响应), 再按窗口截断。 */
export function axisDates(dates: string[] | undefined, fallback: MoodCell[], window: number): string[] {
  const src = dates && dates.length ? dates : fallback.map((c) => c.date)
  return window > 0 ? src.slice(-window) : src
}

/** 单元格按日期索引(轴列渲染用; 缺该交易日的题材显示空位)。 */
export function cellsByDate(cells: MoodCell[]): Map<string, MoodCell> {
  return new Map(cells.map((c) => [c.date, c]))
}
