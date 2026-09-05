import { useEffect, useState } from 'react'
import { useECharts } from '@panwatch/biz-ui/hooks/useECharts'
import { readStockColors } from '@panwatch/biz-ui/lib/stock-colors'
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

/** 桶 → 是否下跌侧(A股惯例): 含“跌”或负区间为跌侧，其余(含平盘)为涨侧。 */
function isDownBucket(bucket: string): boolean {
  if (bucket === '-1~1%') return false
  const neg = ['跌停', '<-5%', '-5~-3%', '-3~-1%']
  return neg.includes(bucket) || bucket.startsWith('-') || bucket.startsWith('<')
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
    const sc = readStockColors()
    // 2026-09-05 交易所大屏式双向镜像柱: 左绿(跌)/右红(涨)，中央0轴，渐变+圆角。
    const ordered = [...items].reverse()
    const downs = ordered.filter((i) => isDownBucket(i.bucket))
    const ups = ordered.filter((i) => !isDownBucket(i.bucket))
    const cats = [...downs.map((i) => i.bucket)].reverse().concat(ups.map((i) => i.bucket))
    chart.setOption({
      grid: { left: 56, right: 56, top: 4, bottom: 4 },
      xAxis: { type: 'value', show: false },
      yAxis: {
        type: 'category',
        data: cats,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { fontSize: 10, color: '#8e8e96' },
      },
      series: [
        {
          type: 'bar',
          name: '跌',
          data: cats.map((c) => {
            const f = downs.find((i) => i.bucket === c)
            return f
              ? {
                  value: -f.count,
                  itemStyle: {
                    color: { type: 'linear', x: 1, y: 0, x2: 0, y2: 0, colorStops: [{ offset: 0, color: sc.down }, { offset: 1, color: sc.down + '55' }] },
                    borderRadius: [2, 0, 0, 2],
                  },
                }
              : null;
          }),
          barWidth: '55%',
          label: {
            show: true,
            position: 'left',
            fontSize: 10,
            fontFamily: 'monospace',
            color: '#c4c4cb',
            formatter: (p: { value: number | null }) => (p.value == null ? '' : String(Math.abs(p.value))),
          },
        },
        {
          type: 'bar',
          name: '涨',
          data: cats.map((c) => {
            const f = ups.find((i) => i.bucket === c)
            return f
              ? {
                  value: f.count,
                  itemStyle: {
                    color: { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [{ offset: 0, color: sc.up }, { offset: 1, color: sc.up + '55' }] },
                    borderRadius: [0, 2, 2, 0],
                  },
                }
              : null;
          }),
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