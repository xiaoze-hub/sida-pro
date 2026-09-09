import { readStockColors, withAlpha } from '@panwatch/biz-ui/lib/stock-colors'

import { computeDrawdownSeries, type EquityPoint } from '@/lib/drawdown'
import { computeRealizedPnlSeries, type TradeLike } from '@/lib/trades'

/** B5.1: 净值曲线 → 回撤曲线(从峰值回撤 %, 0 在顶部)。 */
export function DrawdownChart({ data }: { data: EquityPoint[] }) {
  const series = computeDrawdownSeries(data)
  if (series.length < 2) {
    return <div className="h-40 flex items-center justify-center text-muted-foreground text-sm">暂无足够数据绘制回撤</div>
  }

  const width = 600
  const height = 140
  const pad = { top: 14, right: 20, bottom: 26, left: 60 }
  const w = width - pad.left - pad.right
  const h = height - pad.top - pad.bottom

  const worst = Math.min(...series.map(d => d.dd), -1) // 至少 -1% 留出轴空间
  const points = series.map((d, i) => ({
    x: pad.left + (i / (series.length - 1)) * w,
    y: pad.top + (d.dd / worst) * h, // dd=0 → 顶部; worst → 底部
    ...d,
  }))
  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ')
  const areaD = pathD + ` L${points[points.length - 1].x},${pad.top} L${points[0].x},${pad.top} Z`
  const sc = readStockColors()
  const xIndices = [0, Math.floor(series.length / 2), series.length - 1]

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto" preserveAspectRatio="xMidYMid meet">
      {[{ v: 0, y: pad.top }, { v: worst, y: pad.top + h }].map((t, i) => (
        <g key={i}>
          <line x1={pad.left} x2={width - pad.right} y1={t.y} y2={t.y} stroke="hsl(var(--border))" strokeWidth={0.5} />
          <text x={pad.left - 6} y={t.y + 4} textAnchor="end" fill="hsl(var(--muted-foreground))" fontSize={10}>
            {`${t.v.toFixed(1)}%`}
          </text>
        </g>
      ))}
      <path d={areaD} fill={withAlpha(sc.down, 0.12)} />
      <path d={pathD} fill="none" stroke={sc.down} strokeWidth={2} />
      {xIndices.map(i => (
        <text key={i} x={points[i].x} y={height - 6} textAnchor="middle" fill="hsl(var(--muted-foreground))" fontSize={10}>
          {series[i].date.slice(5)}
        </text>
      ))}
    </svg>
  )
}

/** B5.2: 成交记录 → 已实现盈亏曲线(按平仓日累计, 元)。 */
export function RealizedPnlChart({ trades }: { trades: TradeLike[] }) {
  const series = computeRealizedPnlSeries(trades)
  if (series.length < 2) {
    return <div className="h-40 flex items-center justify-center text-muted-foreground text-sm">暂无足够成交绘制已实现盈亏</div>
  }
  const width = 600
  const height = 140
  const pad = { top: 14, right: 20, bottom: 26, left: 70 }
  const w = width - pad.left - pad.right
  const h = height - pad.top - pad.bottom
  const values = series.map(s => s.pnl)
  const minV = Math.min(...values, 0)
  const maxV = Math.max(...values, 0)
  const range = (maxV - minV) || 1
  const yOf = (v: number) => pad.top + h - ((v - minV) / range) * h
  const points = series.map((s, i) => ({
    x: pad.left + (i / (series.length - 1)) * w,
    y: yOf(s.pnl),
    ...s,
  }))
  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ')
  const zeroY = yOf(0)
  const sc = readStockColors()
  const last = values[values.length - 1]
  const color = last >= 0 ? sc.up : sc.down
  const xIndices = [0, Math.floor(series.length / 2), series.length - 1]

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto" preserveAspectRatio="xMidYMid meet">
      <line x1={pad.left} x2={width - pad.right} y1={zeroY} y2={zeroY} stroke="hsl(var(--border))" strokeDasharray="3 3" />
      {[{ v: maxV, y: yOf(maxV) }, { v: 0, y: zeroY }, { v: minV, y: yOf(minV) }].map((t, i) => (
        <text key={i} x={pad.left - 6} y={t.y + 4} textAnchor="end" fill="hsl(var(--muted-foreground))" fontSize={10}>
          {t.v.toFixed(0)}
        </text>
      ))}
      <path d={pathD} fill="none" stroke={color} strokeWidth={2} />
      {points.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r={2} fill={p.pnl >= 0 ? sc.up : sc.down} />
      ))}
      {xIndices.map(i => (
        <text key={i} x={points[i].x} y={height - 6} textAnchor="middle" fill="hsl(var(--muted-foreground))" fontSize={10}>
          {series[i].date.slice(5)}
        </text>
      ))}
    </svg>
  )
}
