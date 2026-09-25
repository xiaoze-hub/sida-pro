import { useId } from 'react'

import { safeFixed } from '@/lib/format'

interface MarketMoodChartProps {
  /** 情绪温度序列(0-100), 时间升序; 非有限值会被过滤 */
  data: number[]
  width?: number
  height?: number
  /** 0-100 固定域(不按数据自适应), 否则不同窗口无法横向比较 */
  ariaLabel?: string
}

/**
 * 全市场情绪温度曲线(手写 SVG, 无第三方图表库 —— 与 Sparkline 同风格)。
 *
 * 与 Sparkline 的关键差异: **纵轴固定 0-100 且必须显示刻度**。
 * 情绪温度是绝对量(0=冰点/100=沸腾), 若按数据自适应, 近 20 日都在 38-42 时
 * 曲线会被拉成剧烈波动 —— 那是**视觉欺骗**。这里钉死 0-100 并画 50 中轴,
 * 让"读数温和"在视觉上也温和。
 */
export default function MarketMoodChart({
  data,
  width = 1000,
  height = 56,
  ariaLabel,
}: MarketMoodChartProps) {
  const gradId = useId()
  const vals = (data || []).filter((v) => typeof v === 'number' && Number.isFinite(v))
  if (vals.length < 2) {
    return <div className="py-2 text-[10px] text-muted-foreground">情绪温度序列不足 2 个交易日, 暂不画线</div>
  }

  const n = vals.length
  const y = (v: number) => height - (Math.max(0, Math.min(100, v)) / 100) * height
  const x = (i: number) => (i / (n - 1)) * width
  const line = vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${safeFixed(x(i), 1)},${safeFixed(y(v), 1)}`).join(' ')
  const area = `${line} L${width},${height} L0,${height} Z`
  const last = vals[n - 1]

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      width="100%"
      height={height}
      className="block text-primary"
      role="img"
      aria-label={ariaLabel || `情绪温度曲线, 最新 ${last}`}
    >
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="currentColor" stopOpacity="0.22" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      {/* 0-100 固定域: 50 中轴用 hairline, 不喧宾夺主 */}
      <line x1="0" y1={y(50)} x2={width} y2={y(50)} stroke="currentColor" strokeOpacity="0.18" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
      <path d={area} fill={`url(#${gradId})`} stroke="none" />
      <path d={line} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(n - 1)} cy={y(last)} r="2.5" fill="currentColor" />
    </svg>
  )
}
