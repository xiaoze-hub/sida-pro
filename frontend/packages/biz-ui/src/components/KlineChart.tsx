/**
 * KlineChart.tsx (v0.4.40 P1 派活) — Lightweight Charts v5 渲染 K 线.
 *
 * 阶段一 (本版本): 蜡烛 + 十字光标 + 滚轮缩放 + 时间区间切换 (1d/1m/1w/5m/15m/30m/60m/d/w/m).
 * 阶段二 (v0.4.41): 接 MA60/牛马线/GS 买卖点.
 * 阶段三 (v0.4.42): 接资金柱 + L4 事件标注 + 4 开关.
 *
 * 设计要点:
 *   - 旧 InteractiveKline.tsx (自研 SVG) **保留兼容路径**, Quote.tsx 不切换, 等阶段二/三验证完再切.
 *   - 不 import 任何图表组件以外的具体实现; 仅消费: KlineItem + KlinesResponse + initialLayers 状态.
 *   - 时间格式: lightweight-charts 要求 YYYY-MM-DD 字符串 (日 K) 或 unix timestamp (分钟级).
 *     按 interval 切换: 日/周/月用 YYYY-MM-DD, 分钟级用 unix time.
 *   - 缺失数据: KlineItem 为空数组时显示"无数据"占位 (业务硬约束: 禁止编造数字).
 */

import { useEffect, useRef, useState } from 'react'
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts'

import { fetchAPI } from '@panwatch/api'

import {
  KIND_ICON,
  KIND_LABEL,
  type KlineEventKind,
  type KlineEventPoint,
  type KlinePriceLine,
} from '../klineEvents'

/** 资金柱数据(对接后端 fund_flow 字段). 红涨绿跌 + 主净分色. */
export interface FundFlowBar {
  date: string
  /** 明盘净额 (东财大单/特大单, 元) */
  open_net?: number | null
  /** 暗盘净额 (.tck 委托号或 thsdk 逐笔, 元). null=无数据 */
  dark_net?: number | null
}

// 与 InteractiveKline.tsx 顶层类型对齐, 暂时不耦合 (改 one-side 即可)
export interface KlineItem {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  turnover?: number
}

export interface KlinesResponse {
  klines: KlineItem[]
  source?: string
}

export type KlineInterval = '1m' | '5m' | '15m' | '30m' | '60m' | '1d' | '1w' | '1mth'

const INTERVAL_OPTIONS: Array<{ key: KlineInterval; label: string }> = [
  { key: '1m', label: '1分' },
  { key: '5m', label: '5分' },
  { key: '15m', label: '15分' },
  { key: '30m', label: '30分' },
  { key: '60m', label: '60分' },
  { key: '1d', label: '日K' },
  { key: '1w', label: '周K' },
  { key: '1mth', label: '月K' },
]

const DAY_BUCKETS: KlineInterval[] = ['1d', '1w', '1mth']

export default function KlineChart(props: {
  symbol: string
  market: string
  /** 初始周期; 切换后写回 (留给父组件保存 URL 用) */
  initialInterval?: KlineInterval
  /** 初始回看天数; 默认 120 */
  initialDays?: number
  /** 容器高度; 默认 360 */
  height?: number
  /** L4 事件标注 (阶段二: 接 events 标准化层, marker 标在K线上) */
  events?: KlineEventPoint[]
  /** 支撑/压力位 (阶段二: 解套盘位等价位线) */
  supportPressure?: KlinePriceLine[]
  /** 资金柱 (阶段三: 红涨绿跌 + 主净分色) */
  fundFlow?: FundFlowBar[]
  /** 阶段三: 事件种类显隐过滤 (默认全部 true). 设 false 该 kind 不渲染 marker */
  kindsVisible?: Partial<Record<KlineEventKind, boolean>>
  /** 阶段三: 支撑/压力位显隐过滤 */
  priceLinesVisible?: { support?: boolean; pressure?: boolean }
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const markerPluginsRef = useRef<ReturnType<typeof createSeriesMarkers<Time>>[]>([])
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const [interval, setInterval] = useState<KlineInterval>(props.initialInterval || '1d')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>('')
  const [dataLen, setDataLen] = useState(0)

  // ── Lightweight Charts 实例化 ─────────────────────────────
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const chart = createChart(container, {
      layout: {
        background: { color: '#0f172a' },
        textColor: '#cbd5e1',
      },
      width: container.clientWidth,
      height: props.height ?? 360,
      grid: {
        vertLines: { color: '#1e293b' },
        horzLines: { color: '#1e293b' },
      },
      timeScale: {
        timeVisible: !DAY_BUCKETS.includes(interval),
        secondsVisible: false,
        borderColor: '#334155',
      },
      rightPriceScale: {
        borderColor: '#334155',
      },
      crosshair: {
        mode: 1, // magnet
        vertLine: { color: '#64748b', width: 1, style: 3, labelBackgroundColor: '#1e293b' },
        horzLine: { color: '#64748b', width: 1, style: 3, labelBackgroundColor: '#1e293b' },
      },
    })

    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#ef4444',
      downColor: '#22c55e',
      borderVisible: false,
      wickUpColor: '#ef4444',
      wickDownColor: '#22c55e',
    })
    // 资金柱(阶段三): 与 K 线同 scale，叠加在K线下方 30% 高度
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    })
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.7, bottom: 0 },
    })
    chartRef.current = chart
    seriesRef.current = series
    volumeSeriesRef.current = volumeSeries

    // 容器尺寸自适应
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry && chart) {
        chart.applyOptions({ width: entry.contentRect.width })
      }
    })
    observer.observe(container)

    return () => {
      observer.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [props.height])

  // ── 拉数据并 setData ──────────────────────────────────────
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setLoading(true)
      setError('')
      try {
        const days = props.initialDays ?? 120
        const query = `/klines/${encodeURIComponent(props.symbol)}?market=${encodeURIComponent(props.market)}&days=${days}&interval=${encodeURIComponent(interval)}`
        const res = await fetchAPI<KlinesResponse>(query)
        if (cancelled) return
        const kl = res.klines || []
        const data = kl.map((it: KlineItem) => ({
          time: toChartTime(it.date, interval),
          open: it.open,
          high: it.high,
          low: it.low,
          close: it.close,
        }))
        seriesRef.current?.setData(data)
        chartRef.current?.timeScale().fitContent()
        setDataLen(kl.length)
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : '加载K线失败')
          setDataLen(0)
          seriesRef.current?.setData([])
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [props.symbol, props.market, interval, props.initialDays])

  // ── L4 事件 markers + 支撑压力位 price lines + 资金柱 (阶段二+三) ──
  useEffect(() => {
    const chart = chartRef.current
    const series = seriesRef.current
    if (!chart || !series) return

    // 1) L4 事件 markers (v5 用 createSeriesMarkers plugin + kindsVisible 过滤)
    const visible = props.kindsVisible || {}
    const markers = (props.events || [])
      .filter((ev) => visible[ev.kind] !== false)
      .map((ev) => ({
        time: toChartTime(ev.date, interval),
        position: ev.tone === 'down' ? ('belowBar' as const) : ('aboveBar' as const),
        color: ev.tone === 'down' ? '#22c55e' : ev.tone === 'up' ? '#ef4444' : '#64748b',
        shape: 'circle' as const,
        text: KIND_ICON[ev.kind] || KIND_LABEL[ev.kind] || ev.kind,
      }))
    if (markers.length) {
      const markersPlugin = createSeriesMarkers(series, markers)
      markerPluginsRef.current.push(markersPlugin)
    }

    // 2) 支撑压力位 (水平虚线 + priceLinesVisible 过滤)
    const plv = props.priceLinesVisible || {}
    for (const line of props.supportPressure || []) {
      if (line.kind === 'support' && plv.support === false) continue
      if (line.kind === 'pressure' && plv.pressure === false) continue
      series.createPriceLine({
        price: line.price,
        color: line.kind === 'pressure' ? '#ef4444' : '#22c55e',
        lineWidth: 1,
        lineStyle: 2, // dashed
        axisLabelVisible: true,
        title: line.label || line.kind,
      })
    }

    // 3) 资金柱 (阶段三): 红涨绿跌 + 主净分色叠加在 K 线下方
    const volSeries = volumeSeriesRef.current
    if (volSeries && props.fundFlow && props.fundFlow.length > 0) {
      const histData = props.fundFlow.map((bar) => {
        const open = bar.open_net ?? 0
        const dark = bar.dark_net ?? 0
        const net = open + dark
        // 颜色优先级: 主净(明+暗)正红(主力进攻)/负绿(主力撤退); 仅看明盘(无暗盘)用次级色
        let color = '#475569' // 无数据: 灰
        if (dark !== null && dark !== undefined && dark !== 0) {
          color = dark > 0 ? '#dc2626' : '#16a34a' // 主净正红/主净负绿(更深, 突出主力意图)
        } else if (open !== null && open !== undefined && open !== 0) {
          color = open > 0 ? '#f87171' : '#4ade80' // 仅明盘: 浅红/浅绿
        }
        return { time: toChartTime(bar.date, interval), value: net, color }
      })
      volSeries.setData(histData)
    }
  }, [props.events, props.supportPressure, props.fundFlow, props.kindsVisible, props.priceLinesVisible, interval])

  // ── 时间格式转换 ──────────────────────────────────────────
  // lightweight-charts 要求: 日级 YYYY-MM-DD; 分钟级 unix time
  function toChartTime(date: string, intv: KlineInterval): Time {
    if (DAY_BUCKETS.includes(intv)) {
      // 兼容 "2026-09-01" / "2026-09-01T00:00:00" → 截断到日
      return date.substring(0, 10) as Time
    }
    // 分钟级: 兼容 ISO 时间或 YYYY-MM-DD HH:MM:SS
    const t = new Date(date.replace(' ', 'T')).getTime() / 1000
    return (Number.isFinite(t) ? t : 0) as Time
  }

  return (
    <div className="flex flex-col gap-2">
      {/* 周期切换器 */}
      <div className="flex items-center gap-1 flex-wrap">
        {INTERVAL_OPTIONS.map((opt) => (
          <button
            key={opt.key}
            onClick={() => setInterval(opt.key)}
            className={`pxpx-2 py-1 text-xs rounded ${
              interval === opt.key
                ? 'bg-primary text-primary-foreground'
                : 'bg-secondary text-secondary-foreground hover:bg-secondary/'
            }`}
          >
            {opt.label}
          </button>
        ))}
        <span className="ml-2 text-[11px] text-muted-foreground">
          {loading
            ? '加载中…'
            : error
              ? `错误: ${error}`
              : dataLen > 0
                ? `${dataLen} 根K线`
                : '无数据'}
        </span>
      </div>
      {/* 图表容器 */}
      <div
        ref={containerRef}
        className="w-full rounded border border-border/30"
        style={{ minHeight: props.height ?? 360 }}
      />
    </div>
  )
}