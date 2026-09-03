import { useEffect, useState } from 'react'
import { useECharts } from '@panwatch/biz-ui/hooks/useECharts'
import { readStockColors, withAlpha } from '@panwatch/biz-ui/lib/stock-colors'
import { fetchAPI } from '@panwatch/api'
import { safeNum } from '@/lib/format'

interface KlineBar {
  date: string
  close: number
}

interface KlinesResp {
  klines?: KlineBar[]
}

interface ConeProps {
  symbol: string
  lastClose: number
  lastDate: string
  prediction: number[]
  direction: string
  p5?: number[] | null
  p95?: number[] | null
}

/**
 * 预测锥图(P1-5, 终端化): 历史收盘(60日) + 预测中线延伸 + P5-P95 置信带。
 * 未来横轴只标 T+1..T+n, 不编造交易日期(用户幻觉敏感)。
 * 历史拉不到时降级为纯预测段展示, 不阻塞。
 */
export default function ForecastConeChart({ symbol, lastClose, lastDate, prediction, direction, p5, p95 }: ConeProps) {
  const { ref, chartRef } = useECharts()
  const [hist, setHist] = useState<KlineBar[] | null>(null)

  useEffect(() => {
    let alive = true
    setHist(null)
    fetchAPI<KlinesResp>(`/klines/${encodeURIComponent(symbol)}?market=CN&days=60`)
      .then((d) => {
        if (!alive) return
        const bars = (d?.klines || [])
          .filter((b) => safeNum(b.close) != null)
          .slice(-60)
        setHist(bars)
      })
      .catch(() => {
        if (alive) setHist([])
      })
    return () => {
      alive = false
    }
  }, [symbol, lastDate])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || hist === null) return
    const sc = readStockColors()
    const predColor = direction === 'down' ? sc.down : sc.up

    const n = Math.min(
      prediction.length,
      p5?.length ?? prediction.length,
      p95?.length ?? prediction.length,
    )
    const med = prediction.slice(0, n)
    const lo = (p5 ?? []).slice(0, n)
    const hi = (p95 ?? []).slice(0, n)
    const hasBand = lo.length === n && hi.length === n && n > 0

    // 横轴: 历史日期(MM-dd) + T+1..n; 历史收盘只占前段, 预测从 lastClose 起连
    const histLabels = hist.map((b) => b.date.slice(5))
    const futLabels = med.map((_, i) => `T+${i + 1}`)
    const labels = [...histLabels, ...futLabels]

    const histVals: (number | null)[] = hist.map((b) => safeNum(b.close))
    const lc = safeNum(lastClose)
    // 预测线起点 = 基准价(与历史末端衔接), 无历史时起点即基准
    const predVals: (number | null)[] = [
      ...new Array(histVals.length - 1).fill(null),
      ...(histVals.length > 0 ? [histVals[histVals.length - 1]] : []),
      ...med,
    ]
    // band 用堆叠法: 下沿透明占位 + (上沿-下沿)面积
    const bandBase: (number | null)[] = [
      ...new Array(histVals.length).fill(null),
      ...(histVals.length > 0 ? [] : [lc]),
      ...lo,
    ]
    const bandTop: (number | null)[] = [
      ...new Array(histVals.length).fill(null),
      ...(histVals.length > 0 ? [] : [0]),
      ...lo.map((v, i) => Math.max((hi[i] ?? v) - v, 0)),
    ]

    const series: Record<string, unknown>[] = [
      {
        name: '历史收盘',
        type: 'line',
        data: histVals.length > 0 ? [...histVals, ...new Array(med.length).fill(null)] : [],
        showSymbol: false,
        smooth: true,
        lineStyle: { width: 1.5, color: '#8e8e96' },
      },
      ...(hasBand
        ? [
            {
              name: 'P5下沿',
              type: 'line',
              stack: 'band',
              data: bandBase,
              showSymbol: false,
              silent: true,
              lineStyle: { opacity: 0 },
              areaStyle: { opacity: 0 },
            },
            {
              name: 'P95上沿',
              type: 'line',
              stack: 'band',
              data: bandTop,
              showSymbol: false,
              silent: true,
              lineStyle: { opacity: 0 },
              areaStyle: { color: withAlpha(predColor, 0.15) },
            },
          ]
        : []),
      {
        name: '模型预测',
        type: 'line',
        data: predVals,
        showSymbol: true,
        symbolSize: 4,
        smooth: true,
        lineStyle: { width: 2, color: predColor },
        itemStyle: { color: predColor },
        markLine: lc != null
          ? {
              silent: true,
              symbol: 'none',
              label: { show: false },
              lineStyle: { type: 'dashed', color: '#6b7280', width: 1 },
              data: [{ yAxis: lc }],
            }
          : undefined,
      },
    ]

    chart.setOption(
      {
        grid: { left: 46, right: 10, top: 8, bottom: 20 },
        xAxis: {
          type: 'category',
          data: labels,
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { fontSize: 9, color: '#8e8e96', interval: Math.max(Math.floor(labels.length / 8), 0) },
        },
        yAxis: {
          type: 'value',
          scale: true,
          splitLine: { lineStyle: { color: 'rgba(120,120,130,.15)' } },
          axisLabel: { fontSize: 9, color: '#8e8e96' },
        },
        series,
        tooltip: {
          trigger: 'axis',
          valueFormatter: (v: unknown) => (typeof v === 'number' ? v.toFixed(2) : '-'),
        },
      },
      true,
    )
  }, [hist, prediction, direction, p5, p95, lastClose, chartRef])

  if (hist === null) {
    return <div className="h-[220px] animate-pulse bg-muted/40" />
  }
  return (
    <div>
      <div className="mb-1 flex items-center justify-between">
        <span className="text-sm font-medium">预测锥图</span>
        <span className="text-[11px] text-muted-foreground">
          灰线历史收盘{hist.length > 0 ? `(${hist.length}日)` : '(历史暂不可用)'} · 彩线模型预测
          {prediction.length > 0 && p5 && p95 ? ' · 阴影 P5-P95' : ''}
        </span>
      </div>
      <div ref={ref} className="h-[220px] w-full" />
    </div>
  )
}
