/**
 * B5.1/KI-038: 净值曲线 → 回撤序列(时间序列), 供 DrawdownChart 渲染。
 *
 * 回撤 = equity / runningPeak − 1(≤0), 与后端 metrics.max_drawdown 同口径,
 * 但保留每一天的值以形成曲线(后端只给标量)。
 */

export interface EquityPoint {
  date: string
  equity: number
}

export interface DrawdownPoint {
  date: string
  /** 回撤百分比, 0 表示处于新高, -12.3 表示从峰值回撤 12.3% */
  dd: number
}

export function computeDrawdownSeries(data: EquityPoint[]): DrawdownPoint[] {
  const out: DrawdownPoint[] = []
  let peak = Number.NEGATIVE_INFINITY
  for (const p of data) {
    const equity = Number(p?.equity)
    if (!Number.isFinite(equity)) continue
    peak = Math.max(peak, equity)
    const dd = peak > 0 ? (equity / peak - 1) * 100 : 0
    out.push({ date: String(p?.date ?? ''), dd: Number(dd.toFixed(4)) })
  }
  return out
}

export function maxDrawdownOf(series: DrawdownPoint[]): number {
  if (!series.length) return 0
  return series.reduce((acc, p) => Math.min(acc, p.dd), 0)
}
