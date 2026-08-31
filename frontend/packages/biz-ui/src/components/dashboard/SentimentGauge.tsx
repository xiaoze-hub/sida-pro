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

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    const h = metrics?.max_height ?? 0
    const promo = metrics?.promo_rate ?? 0
    const seal = metrics?.seal_rate ?? 0
    const score = Math.min(100, Math.round(h * 15 + promo * 40 + seal * 45))
    const pointerColor = PHASE_COLOR[phase ?? 'accumulating'] ?? '#64748b'
    const label = phase ? (PHASE_LABEL[phase] ?? phase) : '--'

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
          pointer: {
            length: '62%',
            width: 4,
            itemStyle: { color: pointerColor },
          },
          axisTick: { show: false },
          splitLine: { show: false },
          axisLabel: { show: false },
          detail: {
            offsetCenter: [0, '-18%'],
            fontSize: 20,
            fontFamily: 'monospace',
            fontWeight: 'bold',
            formatter: `{v|${score}}\\n{n|市场温度 · ${label}}`,
            rich: {
              v: { fontSize: 22, fontWeight: 'bold', color: pointerColor },
              n: { fontSize: 9, color: '#8e8e96', padding: [4, 0, 0, 0] },
            },
          },
          data: [{ value: score }],
        },
      ],
    })
  }, [phase, metrics, chartRef])

  return <div ref={ref} className="h-[140px] w-full" />
}