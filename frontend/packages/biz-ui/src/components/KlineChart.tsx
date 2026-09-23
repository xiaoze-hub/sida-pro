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

import { useCallback, useEffect, useRef, useState } from 'react'
import { broadcastCrosshair, registerCrosshair } from '../lib/crosshair-sync'
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
import MinutePane from './MinutePane'
import {
  intentLabelFor,
  intentMarkersFor,
  intentPriceLinesFor,
  intentRenderable,
  limitMoveMarkers,
} from '../lib/main-intent'
import type { MainIntentStructured } from '../lib/main-intent-types'
import { safeFixed, toAmount } from '@/lib/format'

import { readStockColors, readChartTheme, maShade, readGsColors, gsColorFor, directionColorFor, activityLevelColor, thresholdLine, readAccentPrimary } from '../lib/stock-colors'
import { dayKey, filterMarkersInBarsRange } from '../lib/chart-markers'
// L3 资金柱的**唯一**净额/分色/时间口径(与 InteractiveKline 共用, 见 lib/fund-bar.ts)
import { fundBarPoint, fundBarTime, DAY_BUCKETS, type FundFlowBar, type KlineInterval } from '../lib/fund-bar'

import {
  KIND_ICON,
  KIND_LABEL,
  normalizeKlineEvents,
  normalizePriceLines,
  type KlineEventKind,
  type KlineEventPoint,
  type KlinePriceLine,
} from '../klineEvents'
// v2.1 §10.2④: 区间统计纯函数(可视区间 → 首末价/涨跌幅/振幅/累计明暗盘/事件数)
import { computeRangeStats, type KlineRangeStats, type RangeBar } from '../lib/range-stats'

export type { KlineRangeStats, RangeBar } from '../lib/range-stats'

// 重导出共享模块的类型/纯函数 —— 既有调用方(`import { fundBarPoint } from '.../KlineChart'`)
// 与既有测试导入路径零改动; 真源统一在 `../lib/fund-bar`。
export type { FundFlowBar, KlineInterval } from '../lib/fund-bar'
export { fundBarPoint, fundBarTime, capitalBarRows } from '../lib/fund-bar'

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

/**
 * `GET /klines/{symbol}/summary` 的**图层子集**(后端 `_build_layer_data` 的输出)。
 *
 * 存在意义(2026-09-18 审计断链修复): 图层数据的唯一真源是 summary 接口, 而 KlineChart
 * 此前只吃**父组件 props** —— 结果是"后端算了、组件画得了、页面没传"(设计稿 §5 落空)。
 * 现在组件自己按需取这一次 summary, 父传了就以父为准(不重复取数)。
 *
 * 诚实约束: 数组元素脏值一律走 `normalizeKlineEvents` / `normalizePriceLines` 过滤,
 * 取不到就整层不画(不编造、不留空壳)。
 */
export interface KlineSummaryLayer {
  gs_signals?: Array<{ date: string; side: 'G' | 'S'; confirmed?: boolean; price?: number | null }> | null
  fund_flow?: FundFlowBar[] | null
  events?: Array<{
    date?: string | null
    kind?: string | null
    label?: string | null
    price?: number | null
  }> | null
  /** 解套/套牢价位线(后端 `unlock_levels_from_chips`); 映射为 §5.3 的支撑/压力虚线 */
  unlock_levels?: Array<{ price?: number | null; kind?: string | null; label?: string | null }> | null
  activity_series?: ActivityPoint[] | null
}

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

// ── L1 趋势 / L2 买卖点 / L5 副图 · 前端自算辅助 (移植自 InteractiveKline v0.4.34) ──

/** L2 GS 买卖点: 日线均线交叉. G=买入(MA5 上穿), S=卖出(MA5 下穿). 实心已确认/空心待确认 */
export interface GsSignalPoint {
  date: string
  side: 'G' | 'S'
  /** 收盘确认=实心(true), 盘中疑似=空心(false). 防"把疑似当确认" */
  confirmed?: boolean
  /**
   * 交叉当日收盘价(后端 `compute_gs_signals` 产出, 见 src/core/gs_strategy.py:210)。
   * 本组件画 marker 不读它, 但 `InteractiveKline` 的同名类型**要求**该字段 —— 让同一次取数
   * 能同时喂两张图, 故在此保留(可选: 缺失时仍可画, 由消费方决定)。
   */
  price?: number | null
}

/** 活跃度序列点 (后端 klines.layer_data.activity_series, 日级, 与 klines 对齐) */
export interface ActivityPoint {
  date: string
  activity: number | null
  /** 大牛/强势/生命/弱 (后端判定, 前端只配色不重判) */
  level?: string | null
}

/** 副图独立 pane（2026-09-20 架构改动，承接 09-19 的"K线与成交量重合"）。
 *
 * 09-19 的修法是"同 pane overlay + 主图价格轴让出底部 30%" —— 能用，但**靠两个数字对齐**：
 * 一旦有人改了其中一处（或加了新 overlay），又会画进同一片像素。用户报的那次重合就是这么来的。
 *
 * 现在改成**真 pane**：副图（成交量/MACD/活跃度/资金柱）挂到 `paneIndex=1`，与主图在
 * **不同画布**上 —— 结构上不可能重合，副图还顺带拿到自己的坐标轴（量柱有了可读的刻度）。
 * 主图价格轴也不再需要给副图让位（原来 bottom=0.32 是"让位"留下的疤），恢复对称留白。
 */
const SUBCHART_PANE = 1
/** 副图占比（stretch factor，与主图按比例分高度） */
const SUBCHART_STRETCH = 0.7
/** 主图价格轴：上下各留 8%（不再给副图让位） */
const PRICE_SCALE_MARGINS = { top: 0.08, bottom: 0.08 } as const


export type KlineSubchart = 'vol' | 'macd' | 'active_ratio' | 'phase' | 'activity'

/** 副图选项(2026-09-04: 抽模块常量, 受控隐藏时不用嵌套括号包 map) */
const SUBCHART_OPTS = [
  ['vol', '成交量'],
  ['macd', 'MACD'],
  ['active_ratio', '买卖比'],
  ['phase', '情绪'],
  ['activity', '活跃度'],
] as const

/** 稳定空数组常量: 图层缺数据时复用同一引用, 避免每帧新数组触发 effect 重跑。 */
const EMPTY_GS: GsSignalPoint[] = []
const EMPTY_FUND: FundFlowBar[] = []
const EMPTY_EVENTS: KlineEventPoint[] = []
const EMPTY_LINES: KlinePriceLine[] = []
const EMPTY_ACTIVITY: ActivityPoint[] = []

/**
 * v2.1 §12: 把 `#RRGGBB` 压暗成半透明色 —— 用于"数据源不可用"的事件图标灰显。
 * Lightweight Charts 的 marker color 直接透传 canvas, 认 `rgba()`;
 * 非 6 位 hex(已是 rgba/颜色名) 原样返回, 不硬造。
 */
function dimHex(hex: string, alpha = 0.35): string {
  const m = /^#([0-9a-f]{6})$/i.exec((hex || '').trim())
  if (!m) return hex
  const n = parseInt(m[1], 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

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
  /**
   * 开启「分时 / K线」切换(P1, 2026-09-18)。
   * 默认 **false**: 只有原先用 InteractiveKline 的页面(指数页/分析详情/模拟盘)才开,
   * 旗舰页 StockWorkbench 行为不受影响。
   */
  enableMinute?: boolean
  /**
   * 主力意图(2026-09-18 P2 补搬): 传了就用传入的; 不传且 `market === 'CN'` 时**自取**
   * `/klines/{symbol}/summary` 的 main_intent_structured —— 与 InteractiveKline 原行为一致
   * (原先靠这个自取渲染"主力意图"图例与箭头, 只看 props 会误判为"无人使用")。
   */
  mainIntent?: MainIntentStructured | null
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
  /** 周期切换回调 (v2.1 §10.2①: 父组件写进 URL ?period=, 刷新/分享不丢状态) */
  onIntervalChange?: (i: KlineInterval) => void
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
  /**
   * v2.1 §10.2④: 区间统计回调 —— 可视区间变化时上报统计读数(或 null = 无区间/无数据)。
   * 计算在组件内做(只有它同时持有 K线/资金柱/事件三份数据), 父组件只负责渲染位置。
   * 父不传 → 不做任何计算。
   */
  onRangeStats?: (stats: KlineRangeStats | null) => void
  /**
   * v2.1 §12: **数据源健康裁决** —— 事件图标所属数据源不可用时, 该 marker **灰显**(压暗)而不是
   * 装作有数据; 悬停读数同时标"(数据源不可用)"。
   *
   * 未传 = 不裁决(保持旧行为: 全部按可用渲染)。裁决表见 `@/hooks/useSourceHealth` 的
   * ICON_SOURCE(拆/⚠撤→tck, 🛡托/🔒压→img, 涨→wencai, 我→shadow, 明盘→tq_moreinfo)。
   * 诚实口径: 请求失败/状态未知一律按不可用处理, 不假设"接口挂了但数据还在"。
   */
  /**
   * v2.1 §6.2「交割单标 K 线」: 本人真实成交买卖点(数据来自 `/api/shadow/trades`)。
   * 买=红箭头标在下方, 卖=绿箭头标在上方(与 §5.2 GS 买卖点同色语义)。
   *
   * **严格口径**: 该日期必须**真有 K 线**才画 —— 周末/节假日的成交不贴到别的柱子上(不给假定位),
   * 也不隐藏(事件仍在列表里可见)。未传 = 不画。
   */
  tradeMarkers?: Array<{ date: string; side: 'buy' | 'sell'; text?: string }>
  sourceReady?: (icon: string) => boolean
  /** v2.1 §12: 灰显原因(悬停 tooltip 用) —— sourceReady 判不可用时给一句人话说明 */
  sourceReason?: (icon: string) => string
  /**
   * v2.1 §10.2③: 十字光标联动回调 —— 除 time/price 外, 追加**该时刻**的明盘/暗盘净额
   * 与事件标签(资金面板据此显示"该时刻"读数, 而不是只有价格)。
   * 缺数据一律 null/[] —— 消费方显示 `--`, 不补 0。
   */
  onCrosshairMove?: (
    param: {
      time: string
      price: number | null
      /** 该时刻所在 K 线的明盘净额(元); 该日无数据 = null */
      mingNet?: number | null
      /** 该时刻所在 K 线的暗盘净额(元); 该日无数据 = null */
      darkNet?: number | null
      /** 该时刻(同日)的事件标签, 如 ['涨停'] */
      events?: string[]
    } | null,
  ) => void
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const markerPluginRef = useRef<ReturnType<typeof createSeriesMarkers<Time>> | null>(null)
  const priceLinesRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([])
  // 持仓成本线 (Phase 0: 与支撑压力线独立管理, 同一切换开关重建逻辑)
  const costLinesRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([])
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // 2026-09-04 P0-3: L3 资金柱独立序列('fund' 轴, 单位元; 成交量是股, 不再共轴)
  const fundSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // 活跃度阈值线 (subchart==='activity' 时建在 volumeSeries 上, 值域与价格独立)
  const activityLinesRef = useRef<ReturnType<ISeriesApi<'Histogram'>['createPriceLine']>[]>([])
  const [interval, setInterval] = useState<KlineInterval>(props.initialInterval || '1d')

  // ── 分时模式(P1): 只保留 "分时/K线" 切换; 取数与四种状态由 MinutePane 自持 ──
  const [mode, setMode] = useState<'kline' | 'minute'>('kline')

  // ── 主力意图(P2 补搬): prop 优先, 没给且是 A 股就自取(失败静默, 不编数据) ──
  const [intentFetched, setIntentFetched] = useState<MainIntentStructured | null>(null)
  useEffect(() => {
    if (props.mainIntent) {
      setIntentFetched(null)
      return
    }
    if (!props.symbol || props.market !== 'CN') {
      setIntentFetched(null)
      return
    }
    let cancelled = false
    fetchAPI<{ main_intent_structured?: MainIntentStructured | null }>(
      `/klines/${encodeURIComponent(props.symbol)}/summary?market=CN`,
    )
      .then((res) => {
        if (!cancelled) setIntentFetched(res.main_intent_structured ?? null)
      })
      .catch(() => {
        if (!cancelled) setIntentFetched(null)
      })
    return () => {
      cancelled = true
    }
  }, [props.symbol, props.market, props.mainIntent])
  const intent = props.mainIntent ?? intentFetched
  const intentLegend = intentLabelFor(intent)
  const intentLinesRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([])

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string>('')
  const [dataLen, setDataLen] = useState(0)

  // ── 图层数据自取(2026-09-18 审计断链修复) ─────────────────────
  // 背景: 后端 /klines/{symbol}/summary 已产出 gs_signals / fund_flow / events /
  // unlock_levels / activity_series, 组件也有图层实现与开关, 但**没有任何页面传这些 props**
  // ⇒ 设计稿 §5 六图层在生产里一根都没画出来(审计发现的最大断链)。
  // 修法: 组件按"父传了就不取"的原则自取一次 summary, 任何使用 KlineChart 的页面自动获得图层。
  const [layer, setLayer] = useState<{
    gsSignals: GsSignalPoint[]
    fundFlow: FundFlowBar[]
    events: KlineEventPoint[]
    priceLines: KlinePriceLine[]
    activitySeries: ActivityPoint[]
  } | null>(null)
  // 父是否接管该分量。用**布尔**入 deps: 内联数组 props 每帧都是新身份, 直接进 deps 会无限取数。
  const ownGs = props.gsSignals === undefined
  const ownFund = props.fundFlow === undefined
  const ownEvents = props.events === undefined
  const ownLines = props.supportPressure === undefined
  const ownActivity = props.activitySeries === undefined
  const needLayer = ownGs || ownFund || ownEvents || ownLines || ownActivity
  // v2.1 §10.2④: 可视区间(订阅只注册一次, 结果落 state 再由纯函数算统计)
  const [visibleRange, setVisibleRange] = useState<{ from: number; to: number } | null>(null)
  // 统计回调走 latest-ref: 父组件内联箭头函数身份每帧变, 进 deps 会造成重复上报
  const onRangeStatsRef = useRef(props.onRangeStats)
  onRangeStatsRef.current = props.onRangeStats
  // 带日期的 K 线(区间统计要按日期与资金柱/事件求交, rawKlinesRef 只有 time/close/volume)
  const rangeBarsRef = useRef<RangeBar[]>([])
  // 2026-09-23: 末根 K 线日期 → 用于诚实标注"是否含今日"(报障: 今日日K不显示,
  // 后端已修桩 bar; 前端同时把"最新一根是哪天"显式写出来, 免得滞后被当成实时)
  const [lastBarDate, setLastBarDate] = useState('')
  // L5 副图: 受控(父传入)或内部自管
  const [subchart, setSubchart] = useState<KlineSubchart>(props.subchart || 'vol')
  /** 运行时 pane 报告: "pane数|各pane高度", 供生产巡检断言(见 publishPanes) */
  const [paneInfo, setPaneInfo] = useState('')
  // 布局验收钩子(2026-09-20, 副图改独立 pane 后): 主图/副图都在 canvas 里, DOM 量不到 ——
  // 把**运行时的 pane 数 + 各 pane 实际高度**挂到容器上, 巡检据此断言
  // "确实是 ≥2 个 pane 且每个都有高度"(即副图真的独立成 pane, 而不是退化成单 pane 叠画)。
  // 为什么不挂配置常量: 配置说"我分了 pane"不等于运行时真分了(旧内核会静默退化) —— 要量运行时的。
  const publishPanes = useCallback(() => {
    const chart = chartRef.current
    if (!chart) return
    try {
      const ps = chart.panes()
      setPaneInfo(`${ps.length}|${ps.map((x) => Math.round(x.getHeight())).join('/')}`)
    } catch { /* noop */ }
  }, [])

  /**
   * KI-056: 每 pane 信息栏 —— 十字光标悬停时展示主图 OHLC + 当前副图读数。
   * 缺字段显示 `--`, 不补 0。
   */
  const [hoverReadout, setHoverReadout] = useState<{
    date: string
    o: number | null
    h: number | null
    l: number | null
    c: number | null
    v: number | null
    /** §10.2③: 该根 K 线的明盘净额(元); 无数据 = null */
    mingNet?: number | null
    /** §10.2③: 该根 K 线的暗盘净额(元); 无数据 = null */
    darkNet?: number | null
    /** §10.2③: 该根 K 线同日事件标签 */
    events?: string[]
    /** P2 补搬(2026-09-18): 该根 K 线的均线读数(IK 曾常显, 迁移后补回; 缺失 null → `--`) */
    ma5?: number | null
    ma10?: number | null
    ma20?: number | null
  } | null>(null)
  /** 均线数组(绘制时算好存这里, 供光标读数取值; 不重复算一遍) */
  const maValuesRef = useRef<{
    ma5: Array<number | null>
    ma10: Array<number | null>
    ma20: Array<number | null>
  }>({ ma5: [], ma10: [], ma20: [] })
  // L1 趋势均线 series (受 layers.trend 控制)
  const maSeriesRef = useRef<Array<ISeriesApi<'Line'>>>([])
  // 原始K线(供 L1 均线 / L5 副图 计算)
  const rawKlinesRef = useRef<Array<{ time: Time; close: number; volume: number }>>([])
  // L5 MACD 副图 series (subchart==='macd' 时渲染)
  const macdSeriesRef = useRef<Array<ISeriesApi<'Line'>>>([])
  // 回调 prop 走 latest-ref: 订阅只注册一次, 调用点取最新回调。父组件内联箭头函数
  // 身份每次渲染都变, 直接进 deps 会导致整图重建(E3 门禁 2026-09-09)。
  const onRangeSelectRef = useRef(props.onRangeSelect)
  onRangeSelectRef.current = props.onRangeSelect
  const onCrosshairMoveRef = useRef(props.onCrosshairMove)
  onCrosshairMoveRef.current = props.onCrosshairMove
  // §12: 数据源裁决也用 ref —— 十字光标订阅 effect 只建一次, 直接读 props 会拿到旧闭包
  const sourceReadyRef = useRef(props.sourceReady)
  sourceReadyRef.current = props.sourceReady

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
    // 主图价格轴: 上下对称留白(不再需要给副图让位 —— 副图已独立 pane)
    series.priceScale().applyOptions({ scaleMargins: PRICE_SCALE_MARGINS })

    // 成交量柱: **独立 pane**(paneIndex=1)。同 pane overlay 时代它靠 margins 挤到底部,
    // 主图没让位就会重合; 现在两图不同画布, 结构上不会重合。
    const volumeSeries = chart.addSeries(
      HistogramSeries,
      {
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
      },
      SUBCHART_PANE,
    )
    // 2026-09-04 P0-3 双单位共轴修复: 资金柱(元) 独立 'fund' 左轴,
    // 此前与成交量(股) 共用 volume 轴 → 轴被撑到 5 亿、量柱压扁(506.43M)。
    const fundSeries = chart.addSeries(
      HistogramSeries,
      {
        priceFormat: { type: 'volume' },
        priceScaleId: 'fund',
        lastValueVisible: false,
        priceLineVisible: false,
      },
      SUBCHART_PANE,
    )
    fundSeries.priceScale().applyOptions({ visible: false } as never)
    try {
      // overlay 轴放左侧(不支持的版本静默忽略, 则与 volume 并列右侧, 不抛)
      fundSeries.priceScale().applyOptions({ position: 'left' } as never)
    } catch { /* noop */ }
    // 副图 pane 占约 30% 高度(与主图按比例分, 窗口缩放时保持比例)
    try {
      chart.panes()[SUBCHART_PANE]?.setStretchFactor(SUBCHART_STRETCH)
    } catch { /* 旧内核不支持 panes 时静默: 退化为单 pane, 不会崩 */ }
    chartRef.current = chart
    seriesRef.current = series
    volumeSeriesRef.current = volumeSeries
    fundSeriesRef.current = fundSeries
    publishPanes()

    // 容器尺寸自适应
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry && chart) {
        chart.applyOptions({ width: entry.contentRect.width })
        publishPanes()
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
      const numOf = (v: unknown): number | null => {
        if (typeof v === 'number' && Number.isFinite(v)) return v
        const ts = (v as { timestamp?: unknown })?.timestamp
        return typeof ts === 'number' && Number.isFinite(ts) ? ts : null
      }
      const nf = range ? numOf(range.from) : null
      const nt = range ? numOf(range.to) : null
      const r =
        range && range.from !== undefined && range.to !== undefined
          ? { from: String(nf ?? range.from), to: String(nt ?? range.to) }
          : null
      onRangeSelectRef.current?.(r)
      // §10.2④: 同一区间喂给统计(数值时间戳; 拿不到数值就置 null → 不报统计)
      setVisibleRange(nf !== null && nt !== null ? { from: nf, to: nt } : null)
    })

    // (3) 十字光标联动: 推 { time, price } 给副图/资金面板 + KI-056 信息栏读数
    //     + **同页多图联动**(2026-09-20): 按 time 广播, 让同页其它图表移到同一根 K 线
    const crosshairSub = {
      apply: ({ time, price }: { time: unknown; price: number }) => {
        try {
          chart.setCrosshairPosition(price, time as Time, series)
        } catch {
          /* 时间不在本图视窗内 → 忽略(不强行跳视窗) */
        }
      },
      clear: () => {
        try {
          chart.clearCrosshairPosition()
        } catch {
          /* ignore */
        }
      },
    }
    const unregisterCrosshair = registerCrosshair(crosshairSub)

    // 双击复位(2026-09-20): 回到"装满已加载区间"的默认视窗 —— 缩放/拖拽后一键还原
    const onDblClick = () => {
      try {
        chart.timeScale().fitContent()
      } catch {
        /* ignore */
      }
    }
    const hostEl = containerRef.current
    hostEl?.addEventListener('dblclick', onDblClick)

    chart.subscribeCrosshairMove((param) => {
      if (!param || !param.time || param.point === undefined) {
        onCrosshairMoveRef.current?.(null)
        setHoverReadout(null)
        broadcastCrosshair(null, crosshairSub)
        return
      }
      const price = series.coordinateToPrice(param.point.y)
      const time =
        typeof param.time === 'number' ? String(param.time) : String(param.time)
      broadcastCrosshair({ time: param.time, price: price ?? 0 }, crosshairSub)
      // §10.2③: 该时刻的资金/事件读数 —— 按"K 线 time 完全相等"定位当日, 不用 ISO 反推
      // (分钟级 K 线的时间戳是本地解析, 用 UTC 反推会错位)。
      const tNum = typeof param.time === 'number' ? param.time : null
      const hitBar = tNum === null ? undefined : rangeBarsRef.current.find((b) => b.time === tNum)
      const hitDate = hitBar?.date ?? null
      const hitFund = hitDate
        ? fundRef.current.find((f) => String(f?.date ?? '').slice(0, 10) === hitDate)
        : undefined
      const hitEvents = hitDate
        ? eventsRef.current
            .filter((e) => e.date.slice(0, 10) === hitDate)
            .map((e) => {
              // §12: 该事件的数据源不可用 → 读数里显式标注, 不让人以为"有图标就是有数据"
              const icon = KIND_ICON[e.kind]
              const ready = sourceReadyRef.current
              return icon && ready && !ready(icon) ? `${e.label}(数据源不可用)` : e.label
            })
        : []
      const mingNet = hitFund
        ? (typeof hitFund.ming_net === 'number' && Number.isFinite(hitFund.ming_net)
            ? hitFund.ming_net
            : typeof hitFund.open_net === 'number' && Number.isFinite(hitFund.open_net)
              ? hitFund.open_net
              : null)
        : null
      const darkNet =
        hitFund && typeof hitFund.dark_net === 'number' && Number.isFinite(hitFund.dark_net)
          ? hitFund.dark_net
          : null
      onCrosshairMoveRef.current?.({ time, price: price ?? null, mingNet, darkNet, events: hitEvents })
      // KI-056: 从 seriesData 取悬停那根的 OHLCV(缺则 null, 不编)
      const bar = param.seriesData?.get(series) as
        | { open?: number; high?: number; low?: number; close?: number }
        | undefined
      const volData = volumeSeriesRef.current
        ? (param.seriesData?.get(volumeSeriesRef.current) as { value?: number } | undefined)
        : undefined
      if (bar && bar.close != null) {
        // 该根 K 线在序列里的序号 → 取同一位置的均线值(算过就复用, 没算过(left null)显示 --)
        const barIdx = rangeBarsRef.current.findIndex((b) => b.time === (tNum as number))
        const mv = maValuesRef.current
        setHoverReadout({
          date: hitDate ?? time,
          o: bar.open ?? null,
          h: bar.high ?? null,
          l: bar.low ?? null,
          c: bar.close,
          v: volData?.value ?? null,
          mingNet,
          darkNet,
          events: hitEvents,
          ma5: barIdx >= 0 ? (mv.ma5[barIdx] ?? null) : null,
          ma10: barIdx >= 0 ? (mv.ma10[barIdx] ?? null) : null,
          ma20: barIdx >= 0 ? (mv.ma20[barIdx] ?? null) : null,
        })
      }
    })

    return () => {
      unregisterCrosshair()
      hostEl?.removeEventListener('dblclick', onDblClick)
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
  }, [props.height, interval, publishPanes])

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
        // 2026-09-05: 过滤 OHLC 含 null/NaN 的行(周末/预测拼接数据常有空值，
        // lightweight-charts setData 遇 null 直接抛 Value is null 全页崩)。
        const valid = kl.filter(
          (it: KlineItem) =>
            Number.isFinite(it.open) &&
            Number.isFinite(it.high) &&
            Number.isFinite(it.low) &&
            Number.isFinite(it.close),
        )
        const data = valid.map((it: KlineItem) => ({
          time: toChartTime(it.date, interval),
          open: it.open,
          high: it.high,
          low: it.low,
          close: it.close,
        }))
        seriesRef.current?.setData(data)
        // L1 均线 / L5 副图 计算源
        rawKlinesRef.current = valid.map((it: KlineItem) => ({
          time: toChartTime(it.date, interval),
          close: it.close,
          volume: it.volume || 0,
        }))
        // v2.1 §10.2④: 区间统计的取数源(带日期 + OHLC)
        setLastBarDate(valid.length ? String((valid[valid.length - 1] as KlineItem).date).slice(0, 10) : '')
        rangeBarsRef.current = valid.map((it: KlineItem) => ({
          time: toChartTime(it.date, interval) as unknown as number,
          date: String(it.date).slice(0, 10),
          open: it.open,
          high: it.high,
          low: it.low,
          close: it.close,
        }))
        chartRef.current?.timeScale().fitContent()
        setDataLen(valid.length)
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

  // ── 图层数据: 按需自取 summary(仅父未接管的分量才发请求) ────────
  useEffect(() => {
    if (!needLayer) {
      setLayer(null)
      return
    }
    let cancelled = false
    const url = `/klines/${encodeURIComponent(props.symbol)}/summary?market=${encodeURIComponent(props.market)}`
    fetchAPI<KlineSummaryLayer>(url)
      .then((res: KlineSummaryLayer | null | undefined) => {
        if (cancelled) return
        const rawGs = Array.isArray(res?.gs_signals) ? res.gs_signals : []
        const gs = rawGs.filter(
          (g): g is GsSignalPoint =>
            !!g && typeof g.date === 'string' && (g.side === 'G' || g.side === 'S'),
        )
        setLayer({
          gsSignals: gs,
          fundFlow: Array.isArray(res?.fund_flow) ? res.fund_flow : [],
          events: normalizeKlineEvents(res?.events),
          priceLines: normalizePriceLines(res?.unlock_levels),
          activitySeries: Array.isArray(res?.activity_series) ? res.activity_series : [],
        })
      })
      .catch(() => {
        // 取不到 = 本次不画图层(降级), 不编造; summary 侧已有自身降级与缓存
        if (!cancelled) setLayer(null)
      })
    return () => {
      cancelled = true
    }
  }, [props.symbol, props.market, needLayer])

  // 有效图层值: 父传优先 → 自取 → 稳定空数组(fetch 用回调整体覆盖, 不用偏函数风格)
  const effGsSignals = props.gsSignals ?? layer?.gsSignals ?? EMPTY_GS
  const effFundFlow = props.fundFlow ?? layer?.fundFlow ?? EMPTY_FUND
  const effEvents = props.events ?? layer?.events ?? EMPTY_EVENTS
  const effPriceLines = props.supportPressure ?? layer?.priceLines ?? EMPTY_LINES
  const effActivitySeries = props.activitySeries ?? layer?.activitySeries ?? EMPTY_ACTIVITY
  // 十字光标 handler 注册一次 → 用 ref 读最新图层数据(与 onRangeSelectRef 同模式)
  const fundRef = useRef<FundFlowBar[]>(effFundFlow)
  fundRef.current = effFundFlow
  const eventsRef = useRef<KlineEventPoint[]>(effEvents)
  eventsRef.current = effEvents
  // §10.2④ 统计也要价位线(区间内出现的支撑/压力), 同样用 ref 供注册一次的回调读取
  const layersPriceLinesRef = useRef<KlinePriceLine[]>(effPriceLines)
  layersPriceLinesRef.current = effPriceLines

  // ── v2.1 §10.2④: 可视区间 → 区间统计(纯计算, 不发请求) ──────────
  // 说明: LWC v5 没有"选段完成"事件, 用 visibleTimeRange 变化作为触发(缩放/拖拽同源),
  // 故本行展示的是"当前可视区间"的统计 —— 与设计稿"拖拽选段"语义一致, 缩放时同样成立。
  const hasStatsCb = props.onRangeStats !== undefined
  useEffect(() => {
    if (!hasStatsCb) return
    if (!visibleRange) {
      onRangeStatsRef.current?.(null)
      return
    }
    const stats = computeRangeStats(
      rangeBarsRef.current,
      fundRef.current,
      eventsRef.current,
      layersPriceLinesRef.current,
      visibleRange.from,
      visibleRange.to,
    )
    onRangeStatsRef.current?.(stats)
  }, [hasStatsCb, visibleRange, effFundFlow, effEvents, effPriceLines, dataLen])

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
      for (const ev of effEvents) {
        if (visible[ev.kind] === false) continue
        const k = `${ev.date}|${ev.kind}`
        const g = grouped.get(k)
        if (g) g.n += 1
        else grouped.set(k, { ev, n: 1 })
      }
      for (const { ev, n } of grouped.values()) {
        const base = KIND_ICON[ev.kind] || KIND_LABEL[ev.kind] || ev.kind
        // §12: 数据源不可用 → 压暗(灰显), 但仍标出位置(不隐藏, 免得看图像"当天没事发生")
        const icon = KIND_ICON[ev.kind]
        const ready = props.sourceReady
        const dimmed = !!icon && !!ready && !ready(icon)
        const toneColor =
          ev.tone === 'down' ? sc.down : ev.tone === 'up' ? sc.up : readChartTheme().neutral
        markers.push({
          time: toChartTime(ev.date, interval),
          position: ev.tone === 'down' ? ('belowBar' as const) : ('aboveBar' as const),
          color: dimmed ? dimHex(toneColor, 0.35) : toneColor,
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
      for (const g of effGsSignals) {
        const isBuy = g.side === 'G'
        markers.push({
          time: toChartTime(g.date, interval),
          position: isBuy ? ('belowBar' as const) : ('aboveBar' as const),
          color: gsColorFor(g.side, gs),
          shape: 'circle' as const,
          size: g.confirmed ? 2 : 0,
          text: g.confirmed ? (isBuy ? 'G' : 'S') : (isBuy ? '○G' : '○S'),
        })
      }
    }
    // §6.2: 本人交割单买卖点(买卖方向来自真实成交, 不是推测)
    if (showSignal && props.tradeMarkers && props.tradeMarkers.length > 0) {
      const gs = readGsColors()
      const bars = rawKlinesRef.current
      const inBars = new Set<number>()
      for (const b of bars) {
        const k = dayKey(b.time)
        if (k !== null) inBars.add(k)
      }
      for (const t of props.tradeMarkers) {
        const time = toChartTime(t.date, interval)
        const k = dayKey(time)
        if (k === null || !inBars.has(k)) continue // 非交易日成交: 不画(不贴柱)
        const isBuy = t.side === 'buy'
        markers.push({
          time,
          position: isBuy ? ('belowBar' as const) : ('aboveBar' as const),
          color: directionColorFor(t.side, gs),
          shape: isBuy ? ('arrowUp' as const) : ('arrowDown' as const),
          size: 1,
          text: t.text || (isBuy ? '买' : '卖'),
        })
      }
    }
    // P2 补搬(2026-09-18): 主力意图箭头 + 涨停/跌停箭头(与 InteractiveKline 同语义/同阈值)
    if (intent) {
      const lastBar = rawKlinesRef.current[rawKlinesRef.current.length - 1]
      markers.push(...(intentMarkersFor(intent, lastBar?.time ?? null, sc.up, sc.down) as never[]))
      markers.push(
        ...(limitMoveMarkers(
          rangeBarsRef.current,
          (d) => toChartTime(d, interval),
          sc.up,
          sc.down,
        ) as never[]),
      )
    }
    // LWC v5: 时间落在首/末根 K 线之外的 marker 会让 setMarkers 抛 "Value is null"
    // → 整页进错误边界(周末/节假日"当天有公告、当天没 K 线"必踩)。裁掉, 不假装定位。
    const safeMarkers = filterMarkersInBarsRange(markers, rawKlinesRef.current.map(b => b.time))
    if (!markerPluginRef.current) markerPluginRef.current = createSeriesMarkers(series, safeMarkers)
    markerPluginRef.current.setMarkers(safeMarkers)

    // 2) 支撑压力位 (L2 买卖点/价位线, showSignal 开关 + priceLinesVisible 过滤)。
    //    先清掉上一轮的价位线, 再按当前开关重建 — 避免切开关导致虚线累积。
    for (const line of priceLinesRef.current) series.removePriceLine(line)
    priceLinesRef.current = []
    const plv = props.priceLinesVisible || {}
    if (showSignal) {
      for (const line of effPriceLines) {
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

    // 2a-2) 主力意图筹码线(P2 补搬): 筹码峰 + 成本带上/下沿
    for (const line of intentLinesRef.current) series.removePriceLine(line)
    intentLinesRef.current = []
    if (showSignal) {
      for (const line of intentPriceLinesFor(intent)) {
        intentLinesRef.current.push(series.createPriceLine(line))
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
      if (subchart === 'activity' && effActivitySeries.length > 0) {
        const histData = effActivitySeries
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
      } else {
        // 2026-09-04 P0-3: 默认档画真正的成交量 bars(股, volume 轴)。
        // 此前缺失: subchart==='vol' + L3 关 = 空白 pane; L3 开 = 资金柱冒充成交量。
        const kl = rawKlinesRef.current
        volSeries.setData(
          kl.map((k, i) => ({
            time: k.time,
            value: k.volume || 0,
            color: (i === 0 ? true : k.close >= kl[i - 1].close) ? sc.up : sc.down,
          })),
        )
      }
      // 2026-09-04 P0-3: L3 资金柱走独立 fund 轴(单位元), 与成交量(股)同显但轴独立。
      // 仅成交量档叠加(活跃度/MACD 档保持独占, 与之前互斥语义一致)。
      // 关 L3 / 无数据 / 非成交量档 → 清空 + 藏轴, 不留残柱。
      const fs = fundSeriesRef.current
      if (fs) {
        if (showCapital && subchart === 'vol' && effFundFlow.length > 0) {
          // 净额与分色由纯函数算(T19 抽出以便单测: 明盘字段名 `ming_net` 曾误读 `open_net`)。
          const theme = readChartTheme()
          fs.setData(effFundFlow.map((bar) => fundBarPoint(bar, interval, sc, theme.nodata)))
          fs.priceScale().applyOptions({ visible: true } as never)
        } else {
          fs.setData([])
          fs.priceScale().applyOptions({ visible: false } as never)
        }
      }
    }
  }, [effEvents, effPriceLines, props.costLines, effFundFlow, effActivitySeries, subchart, props.kindsVisible, props.priceLinesVisible, props.layersVisible, effGsSignals, interval, props.sourceReady, props.tradeMarkers, intent])

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
      maValuesRef.current = { ma5, ma10, ma20 } // 光标读数复用(见 setHoverReadout)
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
        const line = chart.addSeries(
          LineSeries,
          { color: d.color, lineWidth: d.w, priceLineVisible: false, lastValueVisible: false },
          SUBCHART_PANE,
        )
        const pts = rawKlinesRef.current
          .map((k, i) => (d.v[i] == null ? null : { time: k.time, value: d.v[i] as number }))
          .filter((p): p is { time: Time; value: number } => p != null)
        line.setData(pts as never)
        macdSeriesRef.current.push(line)
      }
    }
    // 主动买卖比 / 情绪周期 : 需后端 realtime 数据, Klines 接口无 → 不做假实现, 留给调 UI 切换(灰显"副图数据待接")。
  }, [props.layersVisible?.trend, rawKlinesRef.current.length, subchart, interval])

  // 副图模式切换后 pane 内容变了(MACD 线增删) → 重新上报 pane 高度供巡检读
  useEffect(() => {
    const t = setTimeout(publishPanes, 80)
    return () => clearTimeout(t)
  }, [subchart, publishPanes])

  // ── 时间格式转换 ──────────────────────────────────────────
  // lightweight-charts 要求: 日级 UTCTimestamp(秒); 分钟级 unix time。
  // 单一真源 = `fundBarTime`(lib/fund-bar.ts, 与 L3 资金柱共用) —— 组件内不再另存 `DAY_BUCKETS`/
  // 解析逻辑, 避免"同口径两处各写一份"的漂移(本次 Finding 2 收敛)。
  function toChartTime(date: string, intv: KlineInterval): Time {
    return fundBarTime(date, intv) as Time
  }

  // G/S(含交割单买卖方向)的颜色规则验收钩子: K 线 marker 画在 canvas 上, DOM 里查不到 ——
  // 所以把**解析后的实际颜色**挂到容器上, 生产巡检(terminal_audit)据此断言
  // "G=红系 / S=绿系" 没被改反(改 CSS token 也拦得住)。
  const gsResolved = readGsColors()

  return (
    <div
      className="flex flex-col gap-2"
      data-gs-go={gsResolved.go}
      data-gs-stop={gsResolved.stop}
      data-chart-panes={paneInfo}
    >
      {/* 周期切换器 */}
      <div className="flex items-center gap-1 flex-wrap">
        {INTERVAL_OPTIONS.map((opt) => (
          <button
            key={opt.key}
            onClick={() => {
              setInterval(opt.key)
              // §10.2①: 父组件把周期写进 URL(?period=), 刷新/分享不丢状态
              props.onIntervalChange?.(opt.key)
            }}
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
        {/* 2026-09-04 去重: 父组件受控(props.subchart)时隐藏本行, 以父为准
            (此前两套副图控件互不同步, Quote 还没传 onSubchartChange)。 */}
        {props.subchart === undefined &&
          SUBCHART_OPTS.map(([k, label]) => {
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
        {props.enableMinute && (
          <button
            type="button"
            data-testid="minute-toggle"
            onClick={() => setMode((m) => (m === 'minute' ? 'kline' : 'minute'))}
            className={`px-2 py-1 text-xs rounded ${
              mode === 'minute'
                ? 'bg-primary text-primary-foreground'
                : 'bg-secondary text-secondary-foreground hover:bg-secondary/80'
            }`}
          >
            分时
          </button>
        )}
      </div>
      {/* 图表容器(终端化: 主图裸放, 无框) */}
      <div
        ref={containerRef}
        className={mode === 'minute' && props.enableMinute ? 'hidden' : 'w-full'}
        style={{ minHeight: props.height ?? 360 }}
      />
      {props.enableMinute && mode === 'minute' && (
        <MinutePane symbol={props.symbol} market={props.market} height={props.height ?? 360} />
      )}

      {/* 最新K线新鲜度(2026-09-23): 报障"今日日K不显示"。后端已修(剔桩+补实时),
          前端把"最新一根是哪天"直接写出来 —— 滞后就显式滞后, 绝不把旧数据当实时。
          判据只用日期比对, 不做交易日推算(节假日不误报"今日缺K")。 */}
      {lastBarDate && (
        <div
          data-testid="kline-freshness"
          className="mt-1 text-[11px] text-muted-foreground"
        >
          {lastBarDate === new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date()) ? (
            <>最新K线 {lastBarDate}（含今日盘中）</>
          ) : (
            <>最新K线 {lastBarDate}（今日尚无K线数据）</>
          )}
        </div>
      )}

      {/* GS 买卖点图例(2026-09-23): 起因是用户报障"数智决策显示 S区, 但 K线最新标记是 G" ——
          真因是图层缓存永不失效(已在 summary_cache 修), 但即便数据一致, 用户看到空心 `○S`
          也无从知道它=待确认(盘中价算出的新交叉, 收盘才定死)。补一行图例说明, 免得误读。 */}
      {effGsSignals.length > 0 && (
        <div
          data-testid="gs-legend"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
        >
          <span className="font-medium text-foreground/80">GS 买卖点</span>
          <span>
            <span className="font-mono text-[hsl(var(--gs-go))]">G</span> 买入
          </span>
          <span>
            <span className="font-mono text-[hsl(var(--gs-stop))]">S</span> 卖出
          </span>
          <span>实心 = 已确认(收盘定死)</span>
          <span>空心 ○ = 待确认(盘中价, 收盘才定死)</span>
        </div>
      )}

      {/* 主力意图图例(P2 补搬, 与 InteractiveKline 同口径): 数据不足时显示笔数, 不给方向 */}
      {intentRenderable(intent) && intentLegend && (
        <div
          data-testid="main-intent-legend"
          className="mt-3 rounded-lg border border-rose-500/15 bg-rose-500/5 px-2.5 py-2 text-[11px] text-foreground/80"
        >
          <span className="mr-2 font-medium text-rose-700 dark:text-rose-400">主力意图</span>
          <span className={intentLegend.cls}>{intentLegend.text}</span>
          {typeof intent?.main_net === 'number' && (
            <span className="ml-2 font-mono">
              {toAmount(intent.main_net)}
              {typeof intent.big_net === 'number' &&
                ` (超大${toAmount(intent.big_net)}/大${toAmount(intent.mid_net ?? 0)})`}
            </span>
          )}
          {intent?.chip_peak != null && (
            <span className="ml-2">
              筹码峰 <span className="font-mono">{safeFixed(intent.chip_peak, 2)}</span>
            </span>
          )}
          {intent?.chip_band && (
            <span className="ml-2">
              成本带{' '}
              <span className="font-mono">
                {safeFixed(intent.chip_band.low, 2)}-{safeFixed(intent.chip_band.high, 2)}
              </span>
            </span>
          )}
          {typeof intent?.profit_ratio === 'number' && (
            <span className="ml-2">
              获利 <span className="font-mono">{safeFixed(intent.profit_ratio * 100, 0)}%</span>
            </span>
          )}
        </div>
      )}

      {/* KI-056: 每 pane 信息栏 —— 悬停十字光标时显示主图 OHLC + 成交量; 未悬停显示副图口径 */}
      <div
        data-testid="kline-pane-info"
        className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground font-mono"
      >
        {hoverReadout ? (
          <>
            <span>{hoverReadout.date.slice(0, 10)}</span>
            <span>开 {hoverReadout.o == null ? '--' : safeFixed(hoverReadout.o)}</span>
            <span>高 {hoverReadout.h == null ? '--' : safeFixed(hoverReadout.h)}</span>
            <span>低 {hoverReadout.l == null ? '--' : safeFixed(hoverReadout.l)}</span>
            <span>收 {safeFixed(hoverReadout.c)}</span>
            <span>
              量 {hoverReadout.v == null ? '--' : `${safeFixed(hoverReadout.v / 10000, 1)}万手`}
            </span>
            {/* P2 补搬(2026-09-18): 均线读数(IS 原先常显, 迁移后补回; 缺值显 --) */}
            <span>
              MA5 {hoverReadout.ma5 == null ? '--' : safeFixed(hoverReadout.ma5)}
            </span>
            <span>
              MA10 {hoverReadout.ma10 == null ? '--' : safeFixed(hoverReadout.ma10)}
            </span>
            <span>
              MA20 {hoverReadout.ma20 == null ? '--' : safeFixed(hoverReadout.ma20)}
            </span>
            {/* §10.2③ 光标联动: 该时刻的资金/事件读数(与资金面板同一口径, 缺数据 = --) */}
            <span>明盘 {toAmount(hoverReadout.mingNet)}</span>
            <span>暗盘 {toAmount(hoverReadout.darkNet)}</span>
            {hoverReadout.events && hoverReadout.events.length > 0 && (
              <span className="text-foreground">{hoverReadout.events.join(' · ')}</span>
            )}
          </>
        ) : (
          <span className="text-[10px]">
            悬停查看该根 K 线读数 · 副图: {SUBCHART_OPTS.find(([k]) => k === subchart)?.[1] ?? subchart}
          </span>
        )}
      </div>
    </div>
  )
}