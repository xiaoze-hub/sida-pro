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
  LineSeries,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type SeriesMarker,
} from 'lightweight-charts'

import { fetchAPI } from '@panwatch/api'

import { readStockColors, withAlpha, readChartTheme, maShade, readGsColors, activityLevelColor, thresholdLine, readAccentPrimary } from '../lib/stock-colors'

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

// ── L1 趋势 / L2 买卖点 / L5 副图 · 前端自算辅助 (移植自 InteractiveKline v0.4.34) ──

/** L2 GS 买卖点: 日线均线交叉. G=买入(MA5 上穿), S=卖出(MA5 下穿). 实心已确认/空心待确认 */
export interface GsSignalPoint {
  date: string
  side: 'G' | 'S'
  /** 收盘确认=实心(true), 盘中疑似=空心(false). 防"把疑似当确认" */
  confirmed?: boolean
}

/** 活跃度序列点 (后端 klines.layer_data.activity_series, 日级, 与 klines 对齐) */
export interface ActivityPoint {
  date: string
  activity: number | null
  /** 大牛/强势/生命/弱 (后端判定, 前端只配色不重判) */
  level?: string | null
}

/** L5 副图切换: 成交量 / MACD / 主动买卖比 / 情绪周期 / 活跃度(09-03 三色柱载体) */
export type KlineSubchart = 'vol' | 'macd' | 'active_ratio' | 'phase' | 'activity'

function sma(values: number[], period: number): Array<number | null> {
  if (period <= 1) return values.map((v) => v)
  const out: Array<number | null> = new Array(values.length).fill(null)
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    if (i >= period - 1) out[i] = sum / period
  }
  return out
}

function ema(values: number[], period: number): Array<number | null> {
  const out: Array<number | null> = new Array(values.length).fill(null)
  if (values.length === 0) return out
  const k = 2 / (period + 1)
  let prev: number | null = null
  for (let i = 0; i < values.length; i++) {
    const v = values[i]
    if (prev == null) {
      prev = v
      out[i] = v
      continue
    }
    prev = v * k + prev * (1 - k)
    out[i] = prev
  }
  return out
}

function computeMacd(closes: number[]) {
  const e12 = ema(closes, 12)
  const e26 = ema(closes, 26)
  const macd: Array<number | null> = closes.map((_, i) => {
    const a = e12[i]
    const b = e26[i]
    if (a == null || b == null) return null
    return a - b
  })
  const macdVals = macd.map((v) => (v == null ? 0 : v))
  const signal = ema(macdVals, 9)
  const hist: Array<number | null> = macd.map((v, i) => {
    if (v == null || signal[i] == null) return null
    return v - (signal[i] as number)
  })
  return { macd, signal, hist }
}

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
  /** L2 GS 买卖点 (设计稿 §5.2): 日线均线交叉, 实心已确认/空心待确认 */
  gsSignals?: GsSignalPoint[]
  /** L5 副图切换 (设计稿 §5.1): 成交量/MACD/主动买卖比/情绪周期 */
  subchart?: KlineSubchart
  /** L5 副图切换回调 (父组件持久化到 URL) */
  onSubchartChange?: (s: KlineSubchart) => void
  /** 支撑/压力位 (阶段二: 解套盘位等价位线) */
  supportPressure?: KlinePriceLine[]
  /** 持仓成本线 (Phase 0: portfolio 持仓成本画进 K 线, 替代 ContextCard 占位; 无持仓不传) */
  costLines?: Array<{ price: number; title: string }>
  /** 资金柱 (阶段三: 红涨绿跌 + 主净分色) */
  fundFlow?: FundFlowBar[]
  /** 活跃度副图序列 (09-03: 三色柱载体, subchart==='activity' 时渲染) */
  activitySeries?: ActivityPoint[]
  /** 阶段三: 事件种类显隐过滤 (默认全部 true). 设 false 该 kind 不渲染 marker */
  kindsVisible?: Partial<Record<KlineEventKind, boolean>>
  /**
   * 设计稿 v2.0 §5 + v2.1 §12: 4 开关(图层总控: L1 趋势 / L2 买卖点 / L3 资金柱 / L4 事件)。
   * 用户可单独关整层; 整层关时该层所有 marker/柱/价位线全部隐藏。
   * 不传 = 默认全开。L4 内部仍受 `kindsVisible` 控制每种事件图标的显隐(per-kind)。
   */
  layersVisible?: { trend?: boolean; signal?: boolean; capital?: boolean; event?: boolean }
  /** 阶段三: 支撑/压力位显隐过滤 */
  priceLinesVisible?: { support?: boolean; pressure?: boolean }
  /** v2.1 §10.2: 选段时间回调 (拖拽选段 → 反查资金面板/事件标注) */
  onRangeSelect?: (range: { from: string; to: string } | null) => void
  /** v2.1 §10.2: 十字光标联动回调 (副图/资金面板同步高亮) */
  onCrosshairMove?: (param: { time: string; price: number | null } | null) => void
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const markerPluginRef = useRef<ReturnType<typeof createSeriesMarkers<Time>> | null>(null)
  const priceLinesRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([])
  // 持仓成本线 (Phase 0: 与支撑压力线独立管理, 同一切换开关重建逻辑)
  const costLinesRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([])
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // 活跃度阈值线 (subchart==='activity' 时建在 volumeSeries 上, 值域与价格独立)
  const activityLinesRef = useRef<ReturnType<ISeriesApi<'Histogram'>['createPriceLine']>[]>([])
  const [interval, setInterval] = useState<KlineInterval>(props.initialInterval || '1d')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>('')
  const [dataLen, setDataLen] = useState(0)
  // L5 副图: 受控(父传入)或内部自管
  const [subchart, setSubchart] = useState<KlineSubchart>(props.subchart || 'vol')
  // L1 趋势均线 series (受 layers.trend 控制)
  const maSeriesRef = useRef<Array<ISeriesApi<'Line'>>>([])
  // 原始K线(供 L1 均线 / L5 副图 计算)
  const rawKlinesRef = useRef<Array<{ time: Time; close: number; volume: number }>>([])
  // L5 MACD 副图 series (subchart==='macd' 时渲染)
  const macdSeriesRef = useRef<Array<ISeriesApi<'Line'>>>([])

  // ── Lightweight Charts 实例化 ─────────────────────────────
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const sc = readStockColors()
    // 终端化: 图表主题跟随 light/dark(原硬编码深 slate, light 下突兀)
    const th = readChartTheme()
    const chart = createChart(container, {
      layout: {
        background: { color: th.bg },
        textColor: th.text,
      },
      width: container.clientWidth,
      height: props.height ?? 360,
      grid: {
        vertLines: { color: th.grid },
        horzLines: { color: th.grid },
      },
      timeScale: {
        timeVisible: !DAY_BUCKETS.includes(interval),
        secondsVisible: false,
        borderColor: th.border,
      },
      rightPriceScale: {
        borderColor: th.border,
      },
      crosshair: {
        mode: 1, // magnet
        vertLine: { color: th.crosshair, width: 1, style: 3, labelBackgroundColor: th.labelBg },
        horzLine: { color: th.crosshair, width: 1, style: 3, labelBackgroundColor: th.labelBg },
      },
    })

    const series = chart.addSeries(CandlestickSeries, {
      upColor: sc.up,
      downColor: sc.down,
      borderVisible: false,
      wickUpColor: sc.up,
      wickDownColor: sc.down,
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

    // 主题跟随: <html> class 变化(dark 切换) → 重读 token 并 applyOptions, 不重建图表
    const themeObserver = new MutationObserver(() => {
      const t = readChartTheme()
      chart.applyOptions({
        layout: { background: { color: t.bg }, textColor: t.text },
        grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
        timeScale: { borderColor: t.border },
        rightPriceScale: { borderColor: t.border },
        crosshair: {
          vertLine: { color: t.crosshair, labelBackgroundColor: t.labelBg },
          horzLine: { color: t.crosshair, labelBackgroundColor: t.labelBg },
        },
      })
    })
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style'] })

    // ── v2.1 §10.2 K线大图交互规范 ──
    // (1) 双击还原: fitContent() 全局视角
    chart.subscribeDblClick(() => {
      chart.timeScale().fitContent()
    })

    // (2) 选段时间: v5 lightweight-charts 用 subscribeVisibleTimeRangeChange
    // (subscribeSelection 是 v4 API, v5 已移除). 推给父组件 → Quote.tsx 反查资金面板.
    chart.timeScale().subscribeVisibleTimeRangeChange((range) => {
      const r =
        range && range.from !== undefined && range.to !== undefined
          ? {
              from: String(
                typeof range.from === 'number' ? range.from : (range.from as { timestamp?: number })?.timestamp ?? range.from
              ),
              to: String(
                typeof range.to === 'number' ? range.to : (range.to as { timestamp?: number })?.timestamp ?? range.to
              ),
            }
          : null
      props.onRangeSelect?.(r)
    })

    // (3) 十字光标联动: 推 { time, price } 给副图/资金面板
    chart.subscribeCrosshairMove((param) => {
      if (!param || !param.time || param.point === undefined) {
        props.onCrosshairMove?.(null)
        return
      }
      const price = series.coordinateToPrice(param.point.y)
      const time =
        typeof param.time === 'number' ? String(param.time) : String(param.time)
      props.onCrosshairMove?.({ time, price: price ?? null })
    })

    return () => {
      observer.disconnect()
      themeObserver.disconnect()
      markerPluginRef.current?.setMarkers([])
      markerPluginRef.current = null
      if (seriesRef.current) {
        for (const line of priceLinesRef.current) seriesRef.current.removePriceLine(line)
        for (const line of costLinesRef.current) seriesRef.current.removePriceLine(line)
      }
      priceLinesRef.current = []
      costLinesRef.current = []
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
        // L1 均线 / L5 副图 计算源
        rawKlinesRef.current = kl.map((it: KlineItem) => ({
          time: toChartTime(it.date, interval),
          close: it.close,
          volume: it.volume || 0,
        }))
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
    const sc = readStockColors()

    // 设计稿 v2.0 §5: 4 开关(图层总控) — 整层关掉 → 该层所有标注全部隐藏。
    // 未传 = 默认全开。开关语义:
    //   layer.trend   (L1 趋势)   → K线均线/趋势辅助(当前图无独立趋势序列, 预留)
    //   layer.signal  (L2 买卖点) → 支撑/压力价位线(解套盘位)
    //   layer.capital (L3 资金柱) → fundFlow 资金柱
    //   layer.event   (L4 事件)   → events markers
    const lv = props.layersVisible || {}
    const showEvent = lv.event !== false
    const showCapital = lv.capital !== false
    const showSignal = lv.signal !== false

    // 1) L4 事件 markers (整层 event 开关 + per-kind kindsVisible 双重过滤) + L2 GS 买卖点 markers。
    //    复用单一 plugin, 用 setMarkers 整体替换 — 开关切换时旧 marker 不残留。
    const markers: SeriesMarker<Time>[] = []
    if (showEvent) {
      const visible = props.kindsVisible || {}
      // 2026-09-03 撤单重叠修复: 同 date+kind 多条事件聚合为一个 marker(×N),
      // 避免同一根 K 线上 N 个 marker 完全重合(与 InteractiveKline 同策略)。
      const grouped = new Map<string, { ev: KlineEventPoint; n: number }>()
      for (const ev of props.events || []) {
        if (visible[ev.kind] === false) continue
        const k = `${ev.date}|${ev.kind}`
        const g = grouped.get(k)
        if (g) g.n += 1
        else grouped.set(k, { ev, n: 1 })
      }
      for (const { ev, n } of grouped.values()) {
        const base = KIND_ICON[ev.kind] || KIND_LABEL[ev.kind] || ev.kind
        markers.push({
          time: toChartTime(ev.date, interval),
          position: ev.tone === 'down' ? ('belowBar' as const) : ('aboveBar' as const),
          color: ev.tone === 'down' ? sc.down : ev.tone === 'up' ? sc.up : readChartTheme().neutral,
          shape: 'circle' as const,
          text: n > 1 ? `${base}×${n}` : base,
        })
      }
    }
    // L2 GS 买卖点 (设计稿 §5.2 + 09-03 色收敛): G=买入(--gs-go 红)/S=卖出(--gs-stop 绿),
    // 与 --stock-up/down 同源(同值不同名); 原版 G绿S红, SIDA 按A股惯例 G红S绿, 验收以位置为准。
    // 实心=已确认, 空心=待确认(防疑似当确认)。
    // LC v5 无 circleOutline 形状, 用 size 区分: 实心 size=2(大), 空心 size=0(小) + 文字 ○ 前缀。
    if (showSignal) {
      const gs = readGsColors()
      for (const g of props.gsSignals || []) {
        const isBuy = g.side === 'G'
        markers.push({
          time: toChartTime(g.date, interval),
          position: isBuy ? ('belowBar' as const) : ('aboveBar' as const),
          color: isBuy ? gs.go : gs.stop,
          shape: 'circle' as const,
          size: g.confirmed ? 2 : 0,
          text: g.confirmed ? (isBuy ? 'G' : 'S') : (isBuy ? '○G' : '○S'),
        })
      }
    }
    if (!markerPluginRef.current) markerPluginRef.current = createSeriesMarkers(series, markers)
    markerPluginRef.current.setMarkers(markers)

    // 2) 支撑压力位 (L2 买卖点/价位线, showSignal 开关 + priceLinesVisible 过滤)。
    //    先清掉上一轮的价位线, 再按当前开关重建 — 避免切开关导致虚线累积。
    for (const line of priceLinesRef.current) series.removePriceLine(line)
    priceLinesRef.current = []
    const plv = props.priceLinesVisible || {}
    if (showSignal) {
      for (const line of props.supportPressure || []) {
        if (line.kind === 'support' && plv.support === false) continue
        if (line.kind === 'pressure' && plv.pressure === false) continue
        priceLinesRef.current.push(
          series.createPriceLine({
            price: line.price,
            color: line.kind === 'pressure' ? sc.up : sc.down,
            lineWidth: 1,
            lineStyle: 2, // dashed
            axisLabelVisible: true,
            title: line.label || line.kind,
          }),
        )
      }
    }

    // 2b) 持仓成本线 (Phase 0: 强调橙实线, hover 轴标签显示成本/数量; 无持仓不画)
    for (const line of costLinesRef.current) series.removePriceLine(line)
    costLinesRef.current = []
    if (showSignal) {
      for (const line of props.costLines || []) {
        if (!Number.isFinite(line.price)) continue
        costLinesRef.current.push(
          series.createPriceLine({
            price: line.price,
            color: readAccentPrimary(),
            lineWidth: 1,
            lineStyle: 0, // solid(与支撑压力虚线区分)
            axisLabelVisible: true,
            title: line.title,
          }),
        )
      }
    }

    // 3) 资金柱 (L3 资金柱, showCapital 开关): 红涨绿跌 + 主净分色叠加在 K 线下方。
    //    关掉时 setData([]) 清空 — 否则上一轮的柱会残留。
    //    09-03: subchart==='activity' 时 volumeSeries 改画活跃度三色柱 (与资金柱互斥, 同 pane)。
    const volSeries = volumeSeriesRef.current
    if (volSeries) {
      // 先清活跃度阈值线 (切走档位不残留)
      for (const line of activityLinesRef.current) {
        try { volSeries.removePriceLine(line) } catch { /* noop */ }
      }
      activityLinesRef.current = []
      if (subchart === 'activity' && props.activitySeries && props.activitySeries.length > 0) {
        const histData = props.activitySeries
          .filter((p) => p.activity != null && Number.isFinite(p.activity))
          .map((p) => ({
            time: toChartTime(p.date, interval),
            value: p.activity as number,
            // 档位后端已判, 前端只配色: 大牛紫/强势红/生命绿/弱灰
            color: activityLevelColor(p.level),
          }))
        volSeries.setData(histData)
        // 阈值线 生命1.56/强势3/大牛6 (后端 ai_activity 同值, 前端只画线不重判)
        const thColor = thresholdLine(0.5)
        for (const [price, title] of [[1.56, '生命1.56'], [3, '强势3'], [6, '大牛6']] as const) {
          activityLinesRef.current.push(
            volSeries.createPriceLine({
              price,
              color: thColor,
              lineWidth: 1,
              lineStyle: 2, // dashed
              axisLabelVisible: true,
              title,
            }),
          )
        }
      } else if (subchart === 'activity') {
        // 有档位无序列: 显式清空, 不画阈值线 (不编造)
        volSeries.setData([])
      } else if (showCapital && props.fundFlow && props.fundFlow.length > 0) {
        const histData = props.fundFlow.map((bar) => {
          const open = bar.open_net ?? 0
          const dark = bar.dark_net ?? 0
          const net = open + dark
          // 颜色优先级: 主净(明+暗)正红(主力进攻)/负绿(主力撤退); 仅看明盘(无暗盘)用次级色。
          // 色值统一取 --stock-up/--stock-down 令牌: 暗盘=原色(突出主力), 明盘=55% 透明度次级色
          let color = readChartTheme().nodata // 无数据: 灰
          if (dark !== null && dark !== undefined && dark !== 0) {
            color = dark > 0 ? sc.up : sc.down
          } else if (open !== null && open !== undefined && open !== 0) {
            color = open > 0 ? withAlpha(sc.up, 0.55) : withAlpha(sc.down, 0.55)
          }
          return { time: toChartTime(bar.date, interval), value: net, color }
        })
        volSeries.setData(histData)
      } else {
        volSeries.setData([])
      }
    }
  }, [props.events, props.supportPressure, props.costLines, props.fundFlow, props.activitySeries, subchart, props.kindsVisible, props.priceLinesVisible, props.layersVisible, props.gsSignals, interval])

  // ── L1 趋势均线 (MA5/10/20/60 + 牛马线) + L5 副图 (摆子: 缩放/十字光标/选段 已由上层 effect 生效) ──
  // 设计稿 §5: L1 均线灰阶 + 牛蓝/马橙, 受 layers.trend 开关; L5 副图受 subchart 切换。
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    // ---- L1 均线: 复用单一 ref 数组, 切换时先清后建 ----
    for (const s of maSeriesRef.current) {
      try { chart.removeSeries(s as unknown as ISeriesApi<'Line'>) } catch { /* noop */ }
    }
    maSeriesRef.current = []
    const showTrend = (props.layersVisible?.trend ?? true) !== false
    if (showTrend && rawKlinesRef.current.length) {
      const closes = rawKlinesRef.current.map((k) => k.close)
      const ma5 = sma(closes, 5)
      const ma10 = sma(closes, 10)
      const ma20 = sma(closes, 20)
      const ma60 = sma(closes, 60)
      const defs: Array<{ v: Array<number | null>; color: string; w: 1 | 2 | 3; title: string }> = [
        { v: ma5, color: maShade(0.9), w: 1, title: 'MA5' },
        { v: ma10, color: maShade(0.75), w: 1, title: 'MA10' },
        { v: ma20, color: maShade(0.6), w: 1, title: 'MA20' },
        { v: ma60, color: maShade(0.45), w: 1, title: 'MA60' },
        // 牛线=MA5 蓝 / 马线=MA20 橙 (BBI 简化; 专业图表语义色, 跨主题保留)
        { v: ma5, color: 'rgba(59, 130, 246, 0.95)', w: 3, title: '牛' },
        { v: ma20, color: 'rgba(249, 115, 22, 0.95)', w: 3, title: '马' },
      ]
      for (const d of defs) {
        const line = chart.addSeries(LineSeries, { color: d.color, lineWidth: d.w, priceLineVisible: false, lastValueVisible: false })
        const pts = rawKlinesRef.current
          .map((k, i) => (d.v[i] == null ? null : { time: k.time, value: d.v[i] as number }))
          .filter((p): p is { time: Time; value: number } => p != null)
        line.setData(pts as never)
        maSeriesRef.current.push(line)
      }
    }

    // ---- L5 副图: MACD (vol 由 volumeSeries 承接; macd 用独立 LineSeries 3 条, 叠加到与资金柱同一 pane) ----
    // 设计稿 §5.1: L5 副图 成交量/MACD/主动买卖比/情绪周期. 成交量已有(volumeSeries)。
    // MACD: DIF=sig线(蓝)/DEA=信号线(橙)/柱=hist(CL专用 hist 用 line 简化为差线)
    for (const s of macdSeriesRef.current) {
      try { chart.removeSeries(s as unknown as ISeriesApi<'Line'>) } catch { /* noop */ }
    }
    macdSeriesRef.current = []
    if (subchart === 'macd' && rawKlinesRef.current.length) {
      const closes = rawKlinesRef.current.map((k) => k.close)
      const { macd, signal } = computeMacd(closes)
      const defs: Array<{ v: Array<number | null>; color: string; w: 1 | 2 }> = [
        { v: macd, color: 'rgba(96, 165, 250, 0.95)', w: 1 },   // DIF
        { v: signal, color: 'rgba(251, 146, 60, 0.95)', w: 1 }, // DEA
      ]
      for (const d of defs) {
        const line = chart.addSeries(LineSeries, { color: d.color, lineWidth: d.w, priceLineVisible: false, lastValueVisible: false })
        const pts = rawKlinesRef.current
          .map((k, i) => (d.v[i] == null ? null : { time: k.time, value: d.v[i] as number }))
          .filter((p): p is { time: Time; value: number } => p != null)
        line.setData(pts as never)
        macdSeriesRef.current.push(line)
      }
    }
    // 主动买卖比 / 情绪周期 : 需后端 realtime 数据, Klines 接口无 → 不做假实现, 留给调 UI 切换(灰显"副图数据待接")。
  }, [props.layersVisible?.trend, rawKlinesRef.current.length, props.subchart, interval])

  // ── 时间格式转换 ──────────────────────────────────────────
  // lightweight-charts 要求: 日级 YYYY-MM-DD; 分钟级 unix time
  function toChartTime(date: string, intv: KlineInterval): Time {
    // v0.4.61: LC v5 markers / series 必须用 UTCTimestamp(秒数字), 字符串 "YYYY-MM-DD"
    //   会导致 markers 全部静默不渲染。统一转秒。
    if (DAY_BUCKETS.includes(intv)) {
      const t = new Date(date.substring(0, 10) + 'T00:00:00Z').getTime() / 1000
      return (Number.isFinite(t) ? t : 0) as Time
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
            className={`px-2 py-1 text-xs rounded ${
              interval === opt.key
                ? 'bg-primary text-primary-foreground'
                : 'bg-secondary text-secondary-foreground hover:bg-secondary/80'
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
        {/* L5 副图切换 (设计稿 §5.1): 成交量/MACD/主动买卖比/情绪周期/活跃度 */}
        {(
          [
            ['vol', '成交量'],
            ['macd', 'MACD'],
            ['active_ratio', '买卖比'],
            ['phase', '情绪'],
            ['activity', '活跃度'],
          ] as const
        ).map(([k, label]) => {
          const active = subchart === k
          const disabled = (k === 'active_ratio' || k === 'phase')
          return (
            <button
              key={k}
              onClick={() => {
                setSubchart(k)
                props.onSubchartChange?.(k)
              }}
              title={disabled ? '副图数据待接入(需实时行情)' : undefined}
              className={`px-2 py-1 text-xs rounded disabled:opacity-40 disabled:cursor-not-allowed ${
                active ? 'bg-primary text-primary-foreground' : 'bg-secondary text-secondary-foreground'
              }`}
            >
              {label}
            </button>
          )
        })}
      </div>
      {/* 图表容器(终端化: 主图裸放, 无框) */}
      <div
        ref={containerRef}
        className="w-full"
        style={{ minHeight: props.height ?? 360 }}
      />
    </div>
  )
}