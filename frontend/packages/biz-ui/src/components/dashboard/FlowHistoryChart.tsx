import { useEffect, useState } from 'react'
import echarts from '@panwatch/biz-ui/lib/echarts-core'
import { useECharts } from '@panwatch/biz-ui/hooks/useECharts'
import { fetchAPI } from '@panwatch/api'

/**
 * 主力净流入日内面积图(v0.4.7, Dashboard 大盘资金流区)。
 * v0.5.0: 使用 useECharts hook (ResizeObserver 替代 window.resize)。
 */

interface FlowSnapshot {
  ts: string
  total_main_flow: number
}
type FlowResp = FlowSnapshot[]

/** ts → HH:mm */
function fmtTime(ts: string): string {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export default function FlowHistoryChart() {
  const { ref, chartRef } = useECharts()
  const [rows, setRows] = useState<FlowSnapshot[] | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await fetchAPI<FlowResp>(
          '/market-data/market-capital-flow/history?hours=4',
        )
        if (alive) setRows(Array.isArray(res) ? res : [])
      } catch {
        if (alive) setRows((prev) => prev ?? [])
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 30000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !rows || rows.length === 0) return
    const times = rows.map((r) => fmtTime(r.ts))
    const vals = rows.map((r) => +(r.total_main_flow / 1e8).toFixed(2))
    chart.setOption({
      grid: { left: 44, right: 10, top: 8, bottom: 18 },
      xAxis: {
        type: 'category',
        data: times,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { fontSize: 9, color: '#8e8e96' },
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: 'rgba(120,120,130,.15)' } },
        axisLabel: { fontSize: 9, color: '#8e8e96', formatter: '{value}亿' },
      },
      series: [
        {
          type: 'line',
          data: vals,
          showSymbol: false,
          smooth: true,
          lineStyle: { width: 1.5, color: '#ef4444' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(239,68,68,.30)' },
              { offset: 1, color: 'rgba(239,68,68,.02)' },
            ]),
          },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { type: 'dashed', color: '#6b7280', width: 1 },
            data: [{ yAxis: 0 }],
          },
        },
      ],
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v: unknown) => `${v as number}亿`,
      },
    })
  }, [rows, chartRef])

  if (rows === null) {
    return <div className="h-[150px] animate-pulse rounded-lg bg-accent/10" />
  }
  if (rows.length === 0) {
    return (
      <div className="flex h-[150px] items-center justify-center text-[11px] text-muted-foreground">
        盘中每30秒积累一条 · 刚上线暂无历史
      </div>
    )
  }
  return <div ref={ref} className="h-[150px] w-full" />
}