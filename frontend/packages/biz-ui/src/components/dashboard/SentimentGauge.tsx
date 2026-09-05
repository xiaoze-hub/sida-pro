import { useEffect } from 'react'
import { useECharts } from '@panwatch/biz-ui/hooks/useECharts'

/**
 * 市场温度仪表盘(v0.4.7, Dashboard 情绪周期区)。
 * v0.5.0: 使用 useECharts hook (ResizeObserver 替代 window.resize)。
 */

export interface GaugeMetrics {
  max_height: number | null
  promo_rate: number | null
  seal_rate: number | null
}

/** 阶段 → 指针色(对齐 MarketPhaseCard 配色语义) */
const PHASE_COLOR: Record<string, string> = {
  ice: '#3b82f6',
  ignite: '#06b6d4',
  rally: '#ef4444',
  climax: '#7f1d1d',
  ebb: '#f97316',
  repair: '#6b7280',
  accumulating: '#64748b',
}

const PHASE_LABEL: Record<string, string> = {
  ice: '冰点',
  ignite: '启动',
  rally: '主升',
  climax: '高潮',
  ebb: '退潮',
  repair: '修复',
  accumulating: '积累中',
}

export default function SentimentGauge({
  phase,
  metrics,
}: {
  phase: string | null
  metrics: GaugeMetrics | null
}) {
  const { ref, chartRef } = useECharts()

  // 2026-09-05 中文两行标签改由 HTML 叠加层渲染:
  // ECharts gauge detail 富文本 `\n` 在该版本下不换行(渲染成 `60n市场温度·修复` 挤出一行掉到表外)，
  // HTML 定位完全可控，不依赖图表库换行语义。
  const h = metrics?.max_height ?? 0
  const promo = metrics?.promo_rate ?? 0
  const seal = metrics?.seal_rate ?? 0
  const score = Math.min(100, Math.round(h * 15 + promo * 40 + seal * 45))
  const pointerColor = PHASE_COLOR[phase ?? 'accumulating'] ?? '#64748b'
  const label = phase ? (PHASE_LABEL[phase] ?? phase) : '--'

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    chart.setOption({
      series: [
        {
          type: 'gauge',
          startAngle: 180,
          endAngle: 0,
          min: 0,
          max: 100,
          radius: '95%',
          center: ['50%', '78%'],
          axisLine: {
            lineStyle: {
              width: 10,
              color: [
                [0.2, '#3b82f6'],
                [0.4, '#06b6d4'],
                [0.6, '#f97316'],
                [0.85, '#ef4444'],
                [1, '#7f1d1d'],
              ],
            },
          },
          // 2026-09-05 质感: 进度弧渐变覆盖 + 指针阴影，交易所大屏感。
          progress: {
            show: true,
            width: 10,
            itemStyle: {
              color: {
                type: 'linear',
                x: 0,
                y: 0,
                x2: 1,
                y2: 0,
                colorStops: [
                  { offset: 0, color: '#3b82f6' },
                  { offset: 0.5, color: pointerColor },
                  { offset: 1, color: pointerColor },
                ],
              },
              shadowColor: pointerColor,
              shadowBlur: 8,
            },
          },
          pointer: {
            length: '62%',
            width: 4,
            itemStyle: { color: pointerColor, shadowColor: pointerColor, shadowBlur: 6 },
          },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          // 2026-09-05 detail 交给 HTML 叠加层渲染，此处置空避免双写。
          detail: { show: false },
          data: [{ value: score }],
        },
      ],
    })
  }, [phase, metrics, chartRef])

  return (
    // 2026-09-05 容器加高到 154px: 圆心在 78%(120px)，下方 34px 正好放下两行字，
    // 指针只扫上半圆 → 文字与表盘零重叠。
    <div className="relative h-[154px] w-full">
      <div ref={ref} className="absolute inset-0" />
      {/* 表盘内两行标签: 分数 + 市场温度·阶段，定位圆心之下，指针扫不到 */}
      <div className="pointer-events-none absolute inset-x-0 text-center" style={{ top: 'calc(78% + 6px)' }}>
        <div
          className="font-mono text-[20px] font-bold leading-none tabular-nums"
          style={{ color: pointerColor }}
        >
          {score}
        </div>
        <div className="mt-1 text-[10px] leading-none text-[#8e8e96]">
          市场温度 · {label}
        </div>
      </div>
    </div>
  )
}