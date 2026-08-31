import { useEffect, useState } from 'react'
import { useECharts } from '@panwatch/biz-ui/hooks/useECharts'
import { fetchAPI } from '@panwatch/api'

/**
 * 全市场涨跌分布双向柱状图(v0.4.7, Dashboard 大盘区)。
 * v0.5.0: 使用 useECharts hook (ResizeObserver 替代 window.resize)。
 */

interface BreadthItem {
  bucket: string
  count: number
}
interface BreadthResp {
  count: number
  total: number
  items: BreadthItem[]
  note?: string
}

/** 桶 → 颜色: 负向绿 / 正向红 / 平盘灰(A股惯例) */
function bucketColor(bucket: string): string {
  if (bucket === '-1~1%') return '#6b7280'
  const neg = ['跌停', '<-5%', '-5~-3%', '-3~-1%']
  return neg.includes(bucket) ? '#10b981' : '#ef4444'
}

export default function BreadthDistributionChart() {
  const { ref, chartRef } = useECharts()
  const [items, setItems] = useState<BreadthItem[] | null>(null)
  const [note, setNote] = useState('')

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await fetchAPI<BreadthResp>('/market-data/breadth-distribution')
        if (!alive) return
        setItems(res?.items ?? [])
        setNote(res?.note ?? '')
      } catch {
        if (alive) setItems((prev) => prev ?? [])
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 60000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !items || items.length === 0) return
    const ordered = [...items].reverse()
    chart.setOption({
      grid: { left: 64, right: 40, top: 4, bottom: 4 },
      xAxis: { type: 'value', show: false },
      yAxis: {
        type: 'category',
        data: ordered.map((i) => i.bucket),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { fontSize: 10, color: '#8e8e96' },
      },
      series: [
        {
          type: 'bar',
          data: ordered.map((i) => ({
            value: i.count,
            itemStyle: { color: bucketColor(i.bucket), borderRadius: 2 },
          })),
          barWidth: '55%',
          label: {
            show: true,
            position: 'right',
            fontSize: 10,
            fontFamily: 'monospace',
            color: '#c4c4cb',
          },
        },
      ],
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    })
  }, [items, chartRef])

  if (items === null) {
    return <div className="h-[160px] animate-pulse rounded-lg bg-accent/10" />
  }
  if (items.length === 0 || items.every((i) => i.count === 0)) {
    return (
      <div className="flex h-[160px] items-center justify-center text-[11px] text-muted-foreground">
        {note || '暂无分布数据'}
      </div>
    )
  }
  return (
    <div>
      <div ref={ref} className="h-[160px] w-full" />
    </div>
  )
}