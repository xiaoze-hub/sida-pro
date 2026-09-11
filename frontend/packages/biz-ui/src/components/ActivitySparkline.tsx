import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { useECharts } from '@panwatch/biz-ui/hooks/useECharts'
import { readStockColors } from '@panwatch/biz-ui/lib/stock-colors'

/**
 * 机构活跃度副图(2026-09-11, 决策先锋升级 C)。
 * 数据来自 GET /api/resonance/activity/{symbol}?days=120(逐日活跃度 + 生命/强势/大牛三线)。
 * 三线: 生命线 1.56 / 强势线 3 / 大牛线 6; 共振日(趋势G区+强度+资金) 以红点标注。
 */

interface SeriesItem {
  date: string
  activity: number | null
  level: string | null
  resonance_level?: string | null
}

interface SeriesResp {
  symbol: string
  available: boolean
  reason?: string
  lines?: { life: number; strong: number; bull: number }
  fund_net?: number | null
  count?: number
  items?: SeriesItem[]
}

const DEFAULT_LINES = { life: 1.56, strong: 3, bull: 6 }

function fmtDate(d: string): string {
  const s = String(d).replace(/-/g, '')
  return s.length >= 8 ? `${s.slice(4, 6)}-${s.slice(6, 8)}` : String(d)
}

export default function ActivitySparkline({ symbol, days = 120 }: { symbol: string; days?: number }) {
  const { ref, chartRef } = useECharts()
  const [resp, setResp] = useState<SeriesResp | null>(null)

  useEffect(() => {
    let alive = true
    if (!symbol) return
    void (async () => {
      try {
        const res = await fetchAPI<SeriesResp>(
          `/resonance/activity/${encodeURIComponent(symbol)}?days=${days}`,
          { cacheMode: 'reload' },
        )
        if (alive) setResp(res)
      } catch {
        if (alive) setResp({ symbol, available: false, reason: '读取失败' })
      }
    })()
    return () => {
      alive = false
    }
  }, [symbol, days])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !resp?.available || !resp.items?.length) return
    const lines = resp.lines || DEFAULT_LINES
    const sc = readStockColors()
    const xs = resp.items.map((i) => fmtDate(i.date))
    const ys = resp.items.map((i) => (i.activity == null ? null : Math.round(Number(i.activity) * 100) / 100))
    const marks = resp.items
      .map((i, idx) => ({ i, idx }))
      .filter(({ i }) => i.resonance_level === '强' && i.activity != null)
      .map(({ i, idx }) => ({ coord: [idx, Math.round(Number(i.activity) * 100) / 100], itemStyle: { color: sc.up }, symbolSize: 7 }))
    chart.setOption(
      {
        grid: { left: 34, right: 8, top: 10, bottom: 18 },
        tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => (v == null ? '—' : `${v}`) },
        xAxis: {
          type: 'category',
          data: xs,
          axisTick: { show: false },
          axisLabel: { fontSize: 9, color: '#8e8e96' },
        },
        yAxis: {
          type: 'value',
          splitLine: { lineStyle: { color: 'rgba(120,120,130,.15)' } },
          axisLabel: { fontSize: 9, color: '#8e8e96' },
        },
        series: [
          {
            type: 'line',
            data: ys,
            showSymbol: false,
            lineStyle: { width: 1.5, color: '#60a5fa' },
            markLine: {
              silent: true,
              symbol: 'none',
              label: { show: false },
              lineStyle: { type: 'dashed', width: 1 },
              data: [
                { yAxis: lines.life, lineStyle: { color: '#94a3b8' } },
                { yAxis: lines.strong, lineStyle: { color: '#ed915f' } },
                { yAxis: lines.bull, lineStyle: { color: '#a855f7' } },
              ],
            },
            markPoint: { data: marks, symbol: 'circle', label: { show: false } },
          },
        ],
      },
      true,
    )
  }, [resp, chartRef])

  if (!resp) return <div className="h-[110px] animate-pulse rounded bg-accent/10" />
  if (!resp.available) {
    return (
      <div className="flex h-[110px] items-center justify-center text-[11px] text-muted-foreground">
        {resp.reason || '暂无活跃度序列'}
      </div>
    )
  }
  return (
    <div>
      <div className="mb-1 flex items-center gap-2 text-[10px] text-muted-foreground">
        <span>AI机构活跃度</span>
        <span className="text-[#94a3b8]">生命线 {DEFAULT_LINES.life}</span>
        <span className="text-[#ed915f]">强势线 {DEFAULT_LINES.strong}</span>
        <span className="text-[#a855f7]">大牛线 {DEFAULT_LINES.bull}</span>
        <span className="ml-auto">红点 = 三指标共振日</span>
      </div>
      <div ref={ref} className="h-[110px] w-full" />
    </div>
  )
}
