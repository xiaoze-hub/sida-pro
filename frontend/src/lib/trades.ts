/**
 * B5.2: 模拟盘成交 → 已实现盈亏曲线(按平仓日累计)。
 *
 * 后端 `/api/paper-trading` 的 trades 只有逐笔记录, 这里聚合成"哪天平仓、累计赚亏多少"的
 * 时间序列, 供 RealizedPnlChart 渲染; 与 equity_curve(含浮动盈亏)互补。
 */

export interface TradeLike {
  exit_date?: string | null
  pnl?: number | null
  stock_symbol?: string | null
  strategy_code?: string | null
}

export interface PnlPoint {
  date: string
  /** 截至该日累计已实现盈亏(元) */
  pnl: number
}

export function computeRealizedPnlSeries(trades: TradeLike[]): PnlPoint[] {
  const byDate = new Map<string, number>()
  for (const t of trades ?? []) {
    const date = String(t?.exit_date ?? '').slice(0, 10)
    const pnl = Number(t?.pnl)
    if (!date || !Number.isFinite(pnl)) continue
    byDate.set(date, (byDate.get(date) ?? 0) + pnl)
  }
  const dates = [...byDate.keys()].sort()
  let cum = 0
  return dates.map((date) => {
    cum += byDate.get(date) ?? 0
    return { date, pnl: Number(cum.toFixed(4)) }
  })
}

/** 平仓日盈亏明细(未累计), 供成交点列表/标记使用。 */
export function realizedByDate(trades: TradeLike[]): PnlPoint[] {
  const byDate = new Map<string, number>()
  for (const t of trades ?? []) {
    const date = String(t?.exit_date ?? '').slice(0, 10)
    const pnl = Number(t?.pnl)
    if (!date || !Number.isFinite(pnl)) continue
    byDate.set(date, (byDate.get(date) ?? 0) + pnl)
  }
  return [...byDate.keys()].sort().map((date) => ({
    date,
    pnl: Number((byDate.get(date) ?? 0).toFixed(4)),
  }))
}
