/**
 * 行内迷你走势(2026-09-20 设计系统 P2: 列表行标配 40×16 sparkline, 近 20 日)。
 *
 * 规则(诚实口径):
 *  - 数据不足(少于 3 个有效点) → **不画**, 返回 null(不编造形状, 也不用平线冒充);
 *  - 涨跌色 + **flat 基线**: 基线取首值, 让"相对起点"一眼可读;
 *  - 纯 SVG, 无 canvas、无动画(列表里几十个实例, 不能各自跑动画)。
 */
import { cn } from '@panwatch/base-ui'

/** SVG 坐标取一位小数: 这是**几何**不是展示数字 —— 不走 safeFixed(那是给用户看的数), 也不用 toFixed。 */
const round1 = (v: number) => Math.round(v * 10) / 10

export interface SparklineProps {
  values: (number | null | undefined)[]
  width?: number
  height?: number
  /** 涨跌方向(决定颜色); 缺省按末值 vs 首值自算 */
  direction?: 'up' | 'down' | 'flat'
  className?: string
}

export default function Sparkline({ values, width = 40, height = 16, direction, className }: SparklineProps) {
  const series = (values || []).map((v) => (typeof v === 'number' && Number.isFinite(v) ? v : null))
  const valid = series.filter((v): v is number => v !== null)
  if (valid.length < 3) return null

  const min = Math.min(...valid)
  const max = Math.max(...valid)
  const span = max - min || 1
  const stepX = series.length > 1 ? width / (series.length - 1) : width
  const y = (v: number) => height - 2 - ((v - min) / span) * (height - 4)

  const dir = direction ?? (valid[valid.length - 1] > valid[0] ? 'up' : valid[valid.length - 1] < valid[0] ? 'down' : 'flat')
  const stroke = dir === 'up' ? 'var(--stock-up)' : dir === 'down' ? 'var(--stock-down)' : 'hsl(var(--flat-color))'

  const d = series
    .map((v, i) => (v === null ? null : `${i === 0 || series[i - 1] === null ? 'M' : 'L'}${round1(i * stepX)},${round1(y(v))}`))
    .filter(Boolean)
    .join(' ')
  const baseY = y(valid[0])

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={cn('shrink-0', className)}
      role="img"
      aria-label={`近 ${valid.length} 日走势`}
      data-sparkline={dir}
    >
      {/* flat 基线: 首值位置, hairline */}
      <line x1={0} x2={width} y1={round1(baseY)} y2={round1(baseY)} stroke="hsl(var(--border))" strokeWidth={1} />
      <path d={d} fill="none" stroke={stroke} strokeWidth={1.25} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}
