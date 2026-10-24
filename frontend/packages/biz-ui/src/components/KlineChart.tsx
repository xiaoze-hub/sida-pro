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

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { broadcastCrosshair, registerCrosshair } from '../lib/crosshair-sync'
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  createSeriesMarkers,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type Time,
  type SeriesMarker,
} from 'lightweight-charts'

import { fetchAPI, fetchScenario, type ScenarioMatrixResponse } from '@panwatch/api'
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

import { readStockColors, readChartTheme, maShade, readGsColors, gsColorFor, directionColorFor, activityLevelColor, thresholdLine, readAccentPrimary, readScenarioColors } from '../lib/stock-colors'
import { dayKey, filterMarkersInBarsRange } from '../lib/chart-markers'
// L3 资金柱的**唯一**净额/分色/时间口径(与 InteractiveKline 共用, 见 lib/fund-bar.ts)
import { fundBarPoint, fundBarTime, DAY_BUCKETS, type FundFlowBar, type KlineInterval } from '../lib/fund-bar'
// GS 区间色带(G区紫/S区绿, 规格 §1) 纯逻辑: 由 GS 买卖点序列逐根推区间
import { gsZonePoints, GS_ZONE_COLOR } from '../lib/gs-zones'
// 暗盘资金副图(规格 §2): 红柱=流入/绿柱=流出 + 四组合用法 + L1_approx 诚实标注
import { DARK_POOL_CONFIDENCE, darkPoolBarPoint, darkPoolCombo, type DarkPoolPoint } from '../lib/dark-pool'

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
// K线形态标注(2026-10-10 图层): 纯逻辑(白名单清洗 + 记号映射), 数据来自 /klines/{symbol}/patterns
import {
  normalizePatterns,
  buildPatternMarkers,
  patternHoverLabels,
  PATTERN_DIRECTION_LABEL,
  type PatternMark,
} from '../lib/kline-patterns'

export type { PatternMark } from '../lib/kline-patterns'

// 图表说明(chart-notes, 2026-10-14 看图决策产品件): 逐标记「为什么在那」的**纯逻辑**。
// 数据源 `/klines/{symbol}/chart-notes` —— 与后端图层标记**单一事实源**(AI 读的=图上标的)。
import {
  normalizeChartNotesPayload,
  markerWhy,
  notesForDate,
  notesBarText,
  zoneNoteText,
  noteForMarker,
  type ChartNote,
} from '../lib/chart-notes'

// 情景关键位图层(2026-10-22): 后端情景矩阵 → 主图水平线(纯逻辑, 可独立单测)。
// 数据源 `/klines/{symbol}/scenario` —— 与 chat 工具 `get_scenario_matrix` 同一套计算,
// 前端只画后端算好的关键位, 不重算方向(不预测方向, 只给条件-反应)。
import {
  buildScenarioLines,
  type ScenarioLine,
  type ScenarioLineTone,
} from '../lib/scenario-lines'

// 决策先锋辅助指标(P3 UI 集成, 2026-10-10): 趋势操盘线/牛熊线的三线序列 + 买卖点 marker 纯逻辑。
// 数据源 `/api/indicators/*`; 与形态标注同策略(父传优先 + 自取, 错峰, 显式降级), 但**职责不同**:
// 这里出**线**(前端按 API params 在日线收盘价上展开全序列), 数据面板出**现值读数**。
import {
  MINUTE_DDE_HINT,
  PIONEER_NOT_ADVICE_TEXT,
  minuteDegradeText,
  niuxiongSeries,
  niuxiongSignalMarkers,
  normalizeMinuteBreakthrough,
  normalizeNiuxiong,
  normalizeTrendLine,
  trendLineSeries,
  trendSignalMarkers,
  type MinuteBreakthroughData,
  type NiuxiongData,
  type TrendLineData,
} from '../lib/pioneer-indicators'

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
  /** 今日 bar 性质: 'pg'(库有真今日 bar) / 'live'(读时合成) / 'missing'(缺, 无数据) */
  today_bar?: string
  /** 合成今日 bar 的诚实标注(仅 today_bar==='live' 时给); partial=盘中未定型 */
  live_bar?: { partial?: boolean; source?: string; as_of?: string }
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

/**
 * B1 首屏冷启动(2026-10-05): 慢速图层摘要(`/klines/{symbol}/summary`)的**错峰延迟**与**显式超时**。
 *
 * 背景: summary 冷链(逐笔翻页 + 盘口 thsdk + wencai)冷启动可达 10s+(B1 走查 `/stocks/002361`
 * 首屏 settle ~22.7s), 且是带1 建议条 / 主图图层 / 主力意图 / 盘口资金标签共用的慢接口。
 * 首屏若与关键画布的 `/klines/{symbol}`(K线主数据)在同一拍发出, 会同时在浏览器连接池
 * (HTTP/1.1 同域 6 条)与后端单 worker 队列上把关键请求排在慢活后面 —— 表现为「K 线主图迟迟不出图」。
 *
 * 两件事(只改加载链, 不动布局/图层/口径):
 *  - 错峰: summary 取数延后到首帧之后, 让关键请求**先行**(主图首帧即挂载);
 *  - 显式超时: fetchAPI 默认 20s 在冷启动 10~22s 窗口会**提前掐断** ⇒ 图层/意图整块空白。
 *    显式给到 45s(与 `useStocksData` 对同端点的既有取舍一致), 取不到仍走**显式降级**
 *    (不画图层/不出图例), 不编造。
 */
const SUMMARY_SLOW_LANE_DELAY_MS = 350
const SUMMARY_TIMEOUT_MS = 45000


export type KlineSubchart = 'vol' | 'macd' | 'active_ratio' | 'phase' | 'activity' | 'dark_pool'

/** 副图选项(2026-09-04: 抽模块常量, 受控隐藏时不用嵌套括号包 map)。
 *  2026-10-11: 加「暗盘资金」档(决策先锋副图1 §2, 红柱=流入/绿柱=流出)。 */
const SUBCHART_OPTS = [
  ['vol', '成交量'],
  ['macd', 'MACD'],
  ['active_ratio', '买卖比'],
  ['phase', '情绪'],
  ['activity', '活跃度'],
  ['dark_pool', '暗盘资金'],
] as const

/** 稳定空数组常量: 图层缺数据时复用同一引用, 避免每帧新数组触发 effect 重跑。 */
const EMPTY_GS: GsSignalPoint[] = []
const EMPTY_FUND: FundFlowBar[] = []
const EMPTY_EVENTS: KlineEventPoint[] = []
const EMPTY_LINES: KlinePriceLine[] = []
const EMPTY_ACTIVITY: ActivityPoint[] = []
const EMPTY_PATTERNS: PatternMark[] = []
const EMPTY_NOTES: ChartNote[] = []
const EMPTY_DARKPOOL: DarkPoolPoint[] = []
const EMPTY_SCENARIO_LINES: ScenarioLine[] = []

/**
 * 情景关键位图层(2026-10-22)的语义色 → 实际颜色(canvas 不认 Tailwind 类, 读 CSS 变量)。
 * 买红卖绿(A股口径): 阻力/走强目标=红系(涨), 支撑/走弱目标=绿系(跌)。
 * 走 --scenario-up/down 令牌(与涨跌色分开, 语义各自可演进), 主题切换自动跟随。
 */
function scenarioLineColor(tone: ScenarioLineTone): string {
  const sc = readScenarioColors()
  return tone === 'rose' ? sc.up : sc.down
}

/** 图例文字用的语义色 class(与画布同语义: 红=涨位, 绿=跌位)。 */
function scenarioLineToneClass(tone: ScenarioLineTone): string {
  return tone === 'rose' ? 'text-rose-600 dark:text-rose-400' : 'text-emerald-600 dark:text-emerald-400'
}

/** GS 区间色带主图独立隐藏轴(顶部窄带) + 暗盘资金副图独立隐藏轴。 */
const GSZONE_SCALE = 'gszone'
const DARKPOOL_SCALE = 'darkpool'

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
  mainAccumulationChips?: any
  initialInterval?: KlineInterval
  /** 初始回看天数; 默认 120 */
  initialDays?: number
  /** 容器高度; 默认 360 */
  height?: number
  /** L4 事件标注 (阶段二: 接 events 标准化层, marker 标在K线上) */
  events?: KlineEventPoint[]
  /** L2 GS 买卖点 (设计稿 §5.2): 日线均线交叉, 实心已确认/空心待确认 */
  gsSignals?: GsSignalPoint[]
  /**
   * K线形态标注(2026-10-10): 严格规则识别的形态(证据, 非建议)。
   * 父不传且 `layersVisible.pattern !== false` 时, 组件自取 `GET /klines/{symbol}/patterns`。
   */
  patterns?: PatternMark[]
  /**
   * 威科夫量价结构标注(本土三层滤网 P1): 放量滞涨/弹簧试探/缩量沉淀/放量突破(证据, 非建议)。
   * 父不传且 `layersVisible.wyckoff !== false` 时, 组件自取 `GET /klines/{symbol}/wyckoff`。
   * 与 patterns 同线型(PatternMark), 复用同一套清洗/记号/悬停管线, 独立开关。
   */
  wyckoff?: PatternMark[]
  /**
   * 情景关键位(2026-10-22): 后端情景矩阵的关键位(阻力/支撑/走强目标/走弱目标), 画成主图水平线。
   * 父不传时组件自取 `GET /klines/{symbol}/scenario`(受 `layersVisible.scenario` / 图层开关门控)。
   * 与 chat 工具 `get_scenario_matrix` 同一套计算 —— 前端只画, 不重算方向。
   */
  scenario?: ScenarioMatrixResponse | null
  /**
   * 趋势操盘线(2026-10-10): 红/黄/绿三线 + 买卖点(证据非建议)。
   * 父不传且 `layersVisible.trendLine !== false` 时, 组件自取 `GET /indicators/trend-line/{symbol}`。
   */
  trendLine?: TrendLineData | null
  /**
   * 牛熊线(2026-10-10): 牛/马/买卖线 + 金叉死叉 B/S(买红卖绿)。
   * 父不传且 `layersVisible.niuxiong !== false` 时, 组件自取 `GET /indicators/niuxiong/{symbol}`。
   */
  niuxiong?: NiuxiongData | null
  /**
   * 图表说明(2026-10-14 看图决策产品件): 逐标记「为什么在那」(触发/时点/失效/置信度)。
   * 父不传且 `layersVisible.explain !== false` 时, 组件自取 `GET /klines/{symbol}/chart-notes`。
   * 与后端图层标记**单一事实源**(AI 读的=图上标的); 用于①图例悬停 title ②悬停读数 ③图下说明条。
   */
  chartNotes?: ChartNote[] | null
  /** 当前 GS 标记(区间/待确认)的说明(与推送/战报同源取句); 父不传则用自取的 zoneNote。 */
  zoneNote?: ChartNote | null
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
  /**
   * 暗盘资金副图序列(2026-10-11 决策先锋副图1 §2, subchart==='dark_pool' 时渲染)。
   * 父不传且切到该档时, 组件自取 `GET /klines/{symbol}/pool-flow-series`(懒取数, 不给首屏加债)。
   * 历史日=OHLC 分摊近似, 图上显式标 `L1_approx`。
   */
  darkPoolSeries?: DarkPoolPoint[]
  /** 阶段三: 事件种类显隐过滤 (默认全部 true). 设 false 该 kind 不渲染 marker */
  kindsVisible?: Partial<Record<KlineEventKind, boolean>>
  /**
   * 设计稿 v2.0 §5 + v2.1 §12: 4 开关(图层总控: L1 趋势 / L2 买卖点 / L3 资金柱 / L4 事件)。
   * 用户可单独关整层; 整层关时该层所有 marker/柱/价位线全部隐藏。
   * 不传 = 默认全开。L4 内部仍受 `kindsVisible` 控制每种事件图标的显隐(per-kind)。
   */
  layersVisible?: { trend?: boolean; signal?: boolean; capital?: boolean; event?: boolean; pattern?: boolean; wyckoff?: boolean; trendLine?: boolean; niuxiong?: boolean; explain?: boolean; /** AI/用户标注 */ aiNote?: boolean; /** 情景关键位(2026-10-22) —— **默认关**(需显式 true 才开, 不干扰原有视图) */ scenario?: boolean }
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
  // 情景关键位水平线(2026-10-22): 独立管理 —— 切标的/关图层时整组清掉, 不残留在新标的上
  const scenarioLinesRef = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([])
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // 2026-09-04 P0-3: L3 资金柱独立序列('fund' 轴, 单位元; 成交量是股, 不再共轴)
  const fundSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // GS 区间色带(2026-10-11 规格 §1): 主图顶部窄带(G区紫/S区绿), 独立隐藏轴
  const gsZoneSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // 暗盘资金副图(2026-10-11 规格 §2): 副图 pane, 红柱流入/绿柱流出, 独立隐藏轴
  const darkPoolSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // 活跃度阈值线 (subchart==='activity' 时建在 volumeSeries 上, 值域与价格独立)
  const activityLinesRef = useRef<ReturnType<ISeriesApi<'Histogram'>['createPriceLine']>[]>([])
  const [interval, setInterval] = useState<KlineInterval>(props.initialInterval || '1d')

  useEffect(() => {
    if (props.initialInterval && props.initialInterval !== interval) {
      setInterval(props.initialInterval)
    }
  }, [props.initialInterval, interval])

  // ── 分时模式(P1): 只保留 "分时/K线" 切换; 取数与四种状态由 MinutePane 自持 ──
  const [mode, setMode] = useState<'kline' | 'minute'>('kline')

  // ── 主力意图(P2 补搬): prop 优先, 没给且是 A 股就自取(失败静默, 不编数据) ──
  const [intentFetched, setIntentFetched] = useState<MainIntentStructured | null>(null)
  // L2-B(2026-10-24): 筹码分布默认收起 —— 原默认 true 时 w-36 的浮层常驻压在 K 线右侧 1/3 上,
  // K 线是主角(铁律), 首屏不该被盖住。状态记 localStorage, 用户开过就保持。
  const [chipProfileOpen, setChipProfileOpen] = useState(() => {
    try { return localStorage.getItem('sida_chip_profile') === '1' } catch { return false }
  })
  const [accumulationChipsFetched, setAccumulationChipsFetched] = useState<any>(null)
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
    // 错峰(见 SUMMARY_SLOW_LANE_DELAY_MS 头注): 摘要延后到首帧后, 不抢主图 `/klines/{symbol}` 的带宽/队列。
    const timer = window.setTimeout(() => {
      fetchAPI<{
        main_intent_structured?: MainIntentStructured | null
        main_accumulation_chips?: any
      }>(
        `/klines/${encodeURIComponent(props.symbol)}/summary?market=CN`,
        { timeoutMs: SUMMARY_TIMEOUT_MS },
      )
        .then((res) => {
          if (!cancelled) {
            setIntentFetched(res.main_intent_structured ?? null)
            if (res.main_accumulation_chips) {
              setAccumulationChipsFetched(res.main_accumulation_chips)
            }
          }
        })
        .catch(() => {
          // 显式降级: 取不到 = 本次不出主力意图图例/箭头(不编造, 不留假方向)
          if (!cancelled) setIntentFetched(null)
        })
    }, SUMMARY_SLOW_LANE_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [props.symbol, props.market, props.mainIntent])
  const intent = props.mainIntent ?? intentFetched
  const intentLegend = intentLabelFor(intent)
  const effAccumulationChips = props.mainAccumulationChips ?? accumulationChipsFetched
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
  // 2026-10-12: 合成今日 bar 的标注(partial/source/as_of) → 『盘中未定型』小字。
  // 后端 today_bar==='live' 才给; 定型(18:00 批量)或非合成 → null。
  const [liveBar, setLiveBar] = useState<{ partial?: boolean; source?: string; as_of?: string } | null>(null)
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
    /** K线形态标注(2026-10-10): 该根 K 线命中的形态, 如 ['红三兵(看涨)'] */
    patterns?: string[]
    /** P2 补搬(2026-09-18): 该根 K 线的均线读数(IK 曾常显, 迁移后补回; 缺失 null → `--`) */
    ma5?: number | null
    ma10?: number | null
    ma20?: number | null
    /** §2 暗盘资金(2026-10-11): 该根主力净额(元) + 收盘涨跌(元), 供四组合用法(仅暗盘资金档) */
    poolNet?: number | null
    poolChg?: number | null
    /** 图表说明(2026-10-14): 该根命中标记的「为什么」行(触发/时点/失效); 无 = 空旷 */
    why?: string[]
  } | null>(null)
  /** 均线数组(绘制时算好存这里, 供光标读数取值; 不重复算一遍) */
  const maValuesRef = useRef<{
    ma5: Array<number | null>
    ma10: Array<number | null>
    ma20: Array<number | null>
  }>({ ma5: [], ma10: [], ma20: [] })
  // L1 趋势均线 series (受 layers.trend 控制)
  const maSeriesRef = useRef<Array<ISeriesApi<'Line'>>>([])
  // 决策先锋辅助指标三线 series(趋势操盘线红/黄/绿 + 牛熊线牛/马/买卖; 2026-10-10)
  const trendLineSeriesRef = useRef<Array<ISeriesApi<'Line'>>>([])
  const niuxiongLineSeriesRef = useRef<Array<ISeriesApi<'Line'>>>([])
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

    // GS 区间色带(2026-10-11 规格 §1): 主图顶部窄带(G区紫/S区绿)。独立隐藏轴 +
    // scaleMargins 把值域压到主图顶部 ~12% → 视觉上是"上方短柱带", 不遮 K 线主体。
    const gsZoneSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: GSZONE_SCALE,
      lastValueVisible: false,
      priceLineVisible: false,
    })
    gsZoneSeries.priceScale().applyOptions({ visible: false } as never)
    try {
      gsZoneSeries.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0.88 } } as never)
    } catch { /* 旧内核不支持则铺满主图, 不崩 */ }

    // 暗盘资金副图(2026-10-11 规格 §2): 挂副图 pane, 独立隐藏轴(红柱流入/绿柱流出)。
    const darkPoolSeries = chart.addSeries(
      HistogramSeries,
      { priceScaleId: DARKPOOL_SCALE, lastValueVisible: false, priceLineVisible: false },
      SUBCHART_PANE,
    )
    darkPoolSeries.priceScale().applyOptions({ visible: false } as never)

    // 副图 pane 占约 30% 高度(与主图按比例分, 窗口缩放时保持比例)
    try {
      chart.panes()[SUBCHART_PANE]?.setStretchFactor(SUBCHART_STRETCH)
    } catch { /* 旧内核不支持 panes 时静默: 退化为单 pane, 不会崩 */ }
    chartRef.current = chart
    seriesRef.current = series
    volumeSeriesRef.current = volumeSeries
    fundSeriesRef.current = fundSeries
    gsZoneSeriesRef.current = gsZoneSeries
    darkPoolSeriesRef.current = darkPoolSeries
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
      // K线形态标注(2026-10-10): 该根 K 线命中的形态读数(证据描述, 无买卖建议)
      // 威科夫结构标注(P1)合并进同一悬停读数(同 PatternMark 线型, 名称即中文展示名)
      const hitPatterns = patternHoverLabels([...patternsRef.current, ...wyckoffRef.current], hitDate)
      // 图表说明(2026-10-14): 该根命中标记的「为什么」(触发/时点/失效) —— 与后端 chart-notes 同源
      const hitNotes = hitDate ? notesForDate(chartNotesRef.current, hitDate) : []
      const hitWhy = hitNotes.map(markerWhy)
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
        // §2 暗盘资金(2026-10-11): 该根净额 + 收盘涨跌(元), 供"暗盘资金"档四组合用法读数
        const poolHit = hitDate
          ? darkPoolRef.current.find((p) => String(p.date || '').slice(0, 10) === hitDate)
          : undefined
        const poolNet =
          poolHit && typeof poolHit.main_net === 'number' && Number.isFinite(poolHit.main_net)
            ? poolHit.main_net
            : null
        const prevClose = barIdx > 0 ? (rangeBarsRef.current[barIdx - 1]?.close ?? null) : null
        const poolChg = prevClose != null ? bar.close - prevClose : null
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
          patterns: hitPatterns,
          ma5: barIdx >= 0 ? (mv.ma5[barIdx] ?? null) : null,
          ma10: barIdx >= 0 ? (mv.ma10[barIdx] ?? null) : null,
          ma20: barIdx >= 0 ? (mv.ma20[barIdx] ?? null) : null,
          poolNet,
          poolChg,
          why: hitWhy,
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
        for (const line of scenarioLinesRef.current) seriesRef.current.removePriceLine(line)
      }
      priceLinesRef.current = []
      costLinesRef.current = []
      scenarioLinesRef.current = []
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
        setLiveBar(res.live_bar ?? null)
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
        // 2026-10-24 P0: lightweight-charts 对 setData 的**时间升序**是硬断言,
        // 乱序直接抛 `data must be asc ordered by time` 把整页打进 ErrorBoundary
        // (实测 /stocks/600519: index=120, time=1792368000 < prev 1792627200)。
        // 接口返回顺序不可信(补数/回填/当日合成 bar 都可能插到中间), 这里统一
        // 按日期升序 + 同日去重; 真发生重排时 warn 一次, 便于回头查数据源而不是靠图表崩。
        const sortedKl = [...valid].sort((a: KlineItem, b: KlineItem) => String(a.date).localeCompare(String(b.date)))
        const dedupedKl = sortedKl.filter(
          (it: KlineItem, i: number) => i === 0 || String(it.date).slice(0, 10) !== String(sortedKl[i - 1].date).slice(0, 10),
        )
        if (dedupedKl.length !== kl.length) {
          console.warn('[KlineChart] klines 非升序/含重复日, 已排序去重', { raw: kl.length, kept: dedupedKl.length })
        }
        valid.splice(0, valid.length, ...dedupedKl)
        const data = valid.map((it: KlineItem) => ({
          time: toChartTime(it.date, interval),
          open: it.open,
          high: it.high,
          low: it.low,
          close: it.close,
        }))
        seriesRef.current?.setData(ascendSeries(data, 'candles'))
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
    // 错峰(见 SUMMARY_SLOW_LANE_DELAY_MS 头注): 摘要延后到首帧后, 不抢主图 `/klines/{symbol}`。
    const timer = window.setTimeout(() => {
      fetchAPI<KlineSummaryLayer>(url, { timeoutMs: SUMMARY_TIMEOUT_MS })
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
          if ((res as any)?.main_accumulation_chips) {
            setAccumulationChipsFetched((res as any).main_accumulation_chips)
          }
        })
        .catch(() => {
          // 取不到 = 本次不画图层(显式降级), 不编造; summary 侧已有自身降级与缓存
          if (!cancelled) setLayer(null)
        })
    }, SUMMARY_SLOW_LANE_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [props.symbol, props.market, needLayer])

  // ── K线形态标注自取(2026-10-10) ─────────────────────────────
  // 数据源: GET /klines/{symbol}/patterns(与主图 /klines/{symbol} 同源同口径, 标注与 K 线对齐)。
  // 与其它图层同策略: 父传了 props.patterns 就不取; 取不到 = 本次不画(空数组, 不编造)。
  // 受 `layersVisible.pattern` 门控(整层关 → 不发请求, 也不画)。
  const ownPatterns = props.patterns === undefined
  const showPatternLayer = props.layersVisible?.pattern !== false
  const [patternMarks, setPatternMarks] = useState<PatternMark[] | null>(null)
  useEffect(() => {
    if (!ownPatterns || !showPatternLayer || !props.symbol) {
      setPatternMarks(null)
      return
    }
    let cancelled = false
    // 错峰(与 summary 同因): 形态标注不抢主图 /klines/{symbol} 的带宽/队列。
    const timer = window.setTimeout(() => {
      fetchAPI<{ patterns?: unknown }>(
        `/klines/${encodeURIComponent(props.symbol)}/patterns?market=${encodeURIComponent(props.market)}`,
        { timeoutMs: SUMMARY_TIMEOUT_MS },
      )
        .then((res) => {
          if (!cancelled) setPatternMarks(normalizePatterns(res?.patterns))
        })
        .catch(() => {
          // 显式降级: 取不到 = 本次不画形态(不编造), 也不报错
          if (!cancelled) setPatternMarks([])
        })
    }, SUMMARY_SLOW_LANE_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [props.symbol, props.market, ownPatterns, showPatternLayer])

  // ── 威科夫量价结构标注自取(本土三层滤网 P1) ─────────────────────────
  // 数据源: GET /klines/{symbol}/wyckoff(与主图 /klines/{symbol} 同源同口径, 标注与 K 线对齐)。
  // 与 patterns 同策略: 父传了 props.wyckoff 就不取; 取不到 = 本次不画(空数组, 不编造)。
  // 受 `layersVisible.wyckoff` 门控(整层关 → 不发请求, 也不画)。记号配色只是图上区分,
  // 不是方向结论(后端 verdict 常带 note 免责, 图上不渲染买卖建议)。
  const ownWyckoff = props.wyckoff === undefined
  const showWyckoffLayer = props.layersVisible?.wyckoff !== false
  // AI/用户标注（P1 收尾，2026-10-20）：来自 /api/copilot/annotations，画到图上。
  // 铁律：只画落在当前 K 线区间内的点（filterMarkersInBarsRange 统一裁剪，不贴柱假定位）。
  const [aiNotes, setAiNotes] = useState<{ price?: number | null; ts?: string | null; label: string; color?: string }[]>([])
  const [wyckoffMarks, setWyckoffMarks] = useState<PatternMark[] | null>(null)
  useEffect(() => {
    if (!ownWyckoff || !showWyckoffLayer || !props.symbol) {
      setWyckoffMarks(null)
      return
    }
    let cancelled = false
    // 错峰(与 patterns 同因): 量价结构标注不抢主图 /klines/{symbol} 的带宽/队列。
    const timer = window.setTimeout(() => {
      fetchAPI<{ markers?: unknown }>(
        `/klines/${encodeURIComponent(props.symbol)}/wyckoff?market=${encodeURIComponent(props.market)}`,
        { timeoutMs: SUMMARY_TIMEOUT_MS },
      )
        .then((res) => {
          if (!cancelled) setWyckoffMarks(normalizePatterns(res?.markers))
        })
        .catch(() => {
          // 显式降级: 取不到 = 本次不画结构标注(不编造), 也不报错
          if (!cancelled) setWyckoffMarks([])
        })
    }, SUMMARY_SLOW_LANE_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [props.symbol, props.market, ownWyckoff, showWyckoffLayer])

  // ── 情景关键位自取(2026-10-22) ───────────────────────────────────────────────
  // 数据源: GET /klines/{symbol}/scenario(后端情景矩阵, 与 chat 工具同一套计算)。
  // 与其它图层同策略: 父传了 props.scenario 就不取; 取不到 = 本次不画(空数组, 不编造)。
  // 受 `layersVisible.scenario` 门控 —— **默认关**(必须显式 true 才开), 不干扰原有视图。
  // 后端 available=false(关键位不足)时静默不画, 不弹错。
  //
  // 开关口径(与图例区按钮配套): 父传 `layersVisible.scenario` = 父接管(传 true 开/传 false 关);
  // 父不传 = 组件自持本地开关(**默认关**), 由图例区的按钮切换。
  const ownScenario = props.scenario === undefined
  const [scenarioLocalOn, setScenarioLocalOn] = useState(false)
  const showScenarioLayer =
    props.layersVisible?.scenario === true
      ? true
      : props.layersVisible?.scenario === false
        ? false
        : scenarioLocalOn
  const [scenarioData, setScenarioData] = useState<ScenarioMatrixResponse | null>(null)
  useEffect(() => {
    if (!ownScenario || !showScenarioLayer || !props.symbol) {
      setScenarioData(null)
      return
    }
    let cancelled = false
    // 错峰(与 patterns/wyckoff 同因): 情景层不抢主图 /klines/{symbol} 的带宽/队列。
    const timer = window.setTimeout(() => {
      fetchScenario(props.symbol, props.market)
        .then((res) => {
          if (!cancelled) setScenarioData(res)
        })
    }, SUMMARY_SLOW_LANE_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [props.symbol, props.market, ownScenario, showScenarioLayer])

  // ── 图表说明自取(2026-10-14 看图决策产品件): 逐标记「为什么在那」 ─────────────
  // 数据源: GET /klines/{symbol}/chart-notes(后端与图层标记**单一事实源**)。
  // 与其它图层同策略: 父传了 props.chartNotes 就不取; 取不到 = 本次不展示说明(空, 不编造)。
  // 受 `layersVisible.explain` 门控(整层关 → 不发请求, 也不展示)。
  const ownChartNotes = props.chartNotes === undefined
  const showExplainLayer = props.layersVisible?.explain !== false
  const [chartNotesData, setChartNotesData] = useState<{
    notes: ChartNote[]
    zoneNote: ChartNote | null
  } | null>(null)
  useEffect(() => {
    if (!ownChartNotes || !showExplainLayer || !props.symbol) {
      setChartNotesData(null)
      return
    }
    let cancelled = false
    // 错峰(与 summary/patterns 同因): 图表说明不抢主图 /klines/{symbol} 的带宽/队列。
    const timer = window.setTimeout(() => {
      fetchAPI<unknown>(
        `/klines/${encodeURIComponent(props.symbol)}/chart-notes?market=${encodeURIComponent(props.market)}`,
        { timeoutMs: SUMMARY_TIMEOUT_MS },
      )
        .then((res) => {
          if (!cancelled) setChartNotesData(normalizeChartNotesPayload(res))
        })
        .catch(() => {
          // 显式降级: 取不到 = 本次不展示说明(不编造), 也不报错
          if (!cancelled) setChartNotesData({ notes: [], zoneNote: null })
        })
    }, SUMMARY_SLOW_LANE_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [props.symbol, props.market, ownChartNotes, showExplainLayer])

  // ── 决策先锋辅助指标自取(2026-10-10): 趋势操盘线 + 牛熊线 ─────────────────
  // 与形态标注同策略: 父传优先(own* 布尔, 原子化)、错峰不发抢主图、取不到显式降级(不编造)。
  // 受 `layersVisible.{trendLine,niuxiong}` 门控(整层关 → 不发请求, 也不画)。
  const ownTrendLine = props.trendLine === undefined
  const ownNiuxiong = props.niuxiong === undefined
  const showTrendLineLayer = props.layersVisible?.trendLine !== false
  const showNiuxiongLayer = props.layersVisible?.niuxiong !== false
  const [trendLineData, setTrendLineData] = useState<TrendLineData | null>(null)
  const [niuxiongData, setNiuxiongData] = useState<NiuxiongData | null>(null)
  useEffect(() => {
    const wantTrend = ownTrendLine && showTrendLineLayer && !!props.symbol && props.market === 'CN'
    const wantNx = ownNiuxiong && showNiuxiongLayer && !!props.symbol && props.market === 'CN'
    if (!wantTrend && !wantNx) {
      setTrendLineData(null)
      setNiuxiongData(null)
      return
    }
    let cancelled = false
    // 错峰(与 summary/patterns 同因): 辅助指标不抢主图 `/klines/{symbol}` 的带宽/队列。
    const timer = window.setTimeout(() => {
      const q = `?market=${encodeURIComponent(props.market)}`
      if (wantTrend) {
        fetchAPI<unknown>(`/indicators/trend-line/${encodeURIComponent(props.symbol)}${q}`, { timeoutMs: SUMMARY_TIMEOUT_MS })
          .then((r) => { if (!cancelled) setTrendLineData(normalizeTrendLine(r)) })
          .catch(() => { if (!cancelled) setTrendLineData(null) }) // 显式降级: 取不到 = 不画线(不编造)
      }
      if (wantNx) {
        fetchAPI<unknown>(`/indicators/niuxiong/${encodeURIComponent(props.symbol)}${q}`, { timeoutMs: SUMMARY_TIMEOUT_MS })
          .then((r) => { if (!cancelled) setNiuxiongData(normalizeNiuxiong(r)) })
          .catch(() => { if (!cancelled) setNiuxiongData(null) })
      }
    }, SUMMARY_SLOW_LANE_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [props.symbol, props.market, ownTrendLine, ownNiuxiong, showTrendLineLayer, showNiuxiongLayer])

  // ── 分时突破「突/积」提示条自取(2026-10-10) ────────────────────────────────
  // 缺输入时后端显式 `available=false` + reasons → 前端**照实展示降级文案**(不编造信号)。
  const [minuteBreakthrough, setMinuteBreakthrough] = useState<MinuteBreakthroughData | null>(null)
  useEffect(() => {
    if (!props.symbol || props.market !== 'CN') {
      setMinuteBreakthrough(null)
      return
    }
    let cancelled = false
    const timer = window.setTimeout(() => {
      fetchAPI<unknown>(`/indicators/minute-breakthrough/${encodeURIComponent(props.symbol)}?market=${encodeURIComponent(props.market)}`, { timeoutMs: SUMMARY_TIMEOUT_MS })
        .then((r) => { if (!cancelled) setMinuteBreakthrough(normalizeMinuteBreakthrough(r)) })
        .catch(() => { if (!cancelled) setMinuteBreakthrough(null) })
    }, SUMMARY_SLOW_LANE_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [props.symbol, props.market])

  // ── 暗盘资金副图自取(2026-10-11 规格 §2) ────────────────────────────────────
  // 懒取数: 仅切到「暗盘资金」档才发请求(不给首屏加债); 父传 props.darkPoolSeries 则以父为准。
  // 与其它图层同策略: 取不到 = 空数组显式降级(不画, 不编造), 且带显式超时。
  const ownDarkPool = props.darkPoolSeries === undefined
  const [darkPoolFetched, setDarkPoolFetched] = useState<DarkPoolPoint[] | null>(null)
  useEffect(() => {
    const want = ownDarkPool && !!props.symbol && props.market === 'CN' && subchart === 'dark_pool'
    if (!want) {
      setDarkPoolFetched(null)
      return
    }
    let cancelled = false
    const timer = window.setTimeout(() => {
      fetchAPI<{ series?: DarkPoolPoint[] }>(
        `/klines/${encodeURIComponent(props.symbol)}/pool-flow-series?market=CN&days=60`,
        { timeoutMs: SUMMARY_TIMEOUT_MS },
      )
        .then((r) => {
          if (!cancelled) setDarkPoolFetched(Array.isArray(r?.series) ? r.series : [])
        })
        .catch(() => {
          // 显式降级: 取不到 = 本次不画暗盘资金(不编造)
          if (!cancelled) setDarkPoolFetched([])
        })
    }, SUMMARY_SLOW_LANE_DELAY_MS)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [props.symbol, props.market, ownDarkPool, subchart])

  // 有效图层值: 父传优先 → 自取 → 稳定空数组(fetch 用回调整体覆盖, 不用偏函数风格)
  const effGsSignals = props.gsSignals ?? layer?.gsSignals ?? EMPTY_GS
  const effFundFlow = props.fundFlow ?? layer?.fundFlow ?? EMPTY_FUND
  const effEvents = props.events ?? layer?.events ?? EMPTY_EVENTS
  const effPriceLines = props.supportPressure ?? layer?.priceLines ?? EMPTY_LINES
  const effActivitySeries = props.activitySeries ?? layer?.activitySeries ?? EMPTY_ACTIVITY
  const effPatterns = props.patterns ?? patternMarks ?? EMPTY_PATTERNS
  // 威科夫量价结构(本土三层滤网 P1): 父传优先 → 自取 → 稳定空数组(与 patterns 同策略)
  const effWyckoff = props.wyckoff ?? wyckoffMarks ?? EMPTY_PATTERNS
  // 情景关键位(2026-10-22): 父传优先 → 自取 → 稳定空数组; 清洗/去重/排序全在 lib/scenario-lines(纯函数)
  const effScenarioMatrix = props.scenario ?? scenarioData
  const effScenarioLines = useMemo(
    () => (effScenarioMatrix ? buildScenarioLines(effScenarioMatrix) : EMPTY_SCENARIO_LINES),
    [effScenarioMatrix],
  )
  // 决策先锋辅助指标: 父传优先 → 自取(可能为 null = 无数据/降级, 图层据此不画)
  const effTrendLine = props.trendLine ?? trendLineData
  const effNiuxiong = props.niuxiong ?? niuxiongData
  // 图表说明: 父传优先 → 自取(空 = 本次不展示说明, 不编造)
  const effChartNotes = props.chartNotes ?? chartNotesData?.notes ?? EMPTY_NOTES
  const effZoneNote = props.zoneNote ?? chartNotesData?.zoneNote ?? null
  // 暗盘资金序列: 父传优先 → 自取(切到该档才取; null=未取, 空数组=降级不画)
  const effDarkPool = props.darkPoolSeries ?? darkPoolFetched ?? EMPTY_DARKPOOL
  // 十字光标 handler 注册一次 → 用 ref 读最新图层数据(与 onRangeSelectRef 同模式)
  const fundRef = useRef<FundFlowBar[]>(effFundFlow)
  fundRef.current = effFundFlow
  const darkPoolRef = useRef<DarkPoolPoint[]>(effDarkPool)
  darkPoolRef.current = effDarkPool
  const eventsRef = useRef<KlineEventPoint[]>(effEvents)
  eventsRef.current = effEvents
  const patternsRef = useRef<PatternMark[]>(effPatterns)
  patternsRef.current = effPatterns
  const wyckoffRef = useRef<PatternMark[]>(effWyckoff)
  wyckoffRef.current = effWyckoff
  const chartNotesRef = useRef<ChartNote[]>(effChartNotes)
  chartNotesRef.current = effChartNotes
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

  // AI 标注取数（symbol/周期变化即刷新）；失败静默 —— 标注是增强，不该打断看图。
  useEffect(() => {
    const sym = (props.symbol || '').trim()
    if (!sym) {
      setAiNotes([])
      return
    }
    let cancelled = false
    fetchAPI<{ price?: number | null; ts?: string | null; label: string; color?: string }[]>(
      `/copilot/annotations?symbol=${encodeURIComponent(sym)}&market=${encodeURIComponent(props.market || 'CN')}&period=${encodeURIComponent(interval)}`,
    )
      .then((rows) => {
        if (!cancelled) setAiNotes(Array.isArray(rows) ? rows : [])
      })
      .catch(() => {
        if (!cancelled) setAiNotes([])
      })
    return () => {
      cancelled = true
    }
  }, [props.symbol, props.market, interval])

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
    // L2 GS 买卖点 (设计稿 §5.2 + 09-03 色收敛, 2026-10-11 规格 §1 校正为 ▲/▼):
    //   G=机会/买入(--gs-go 红) ▲ 标在下; S=风险/卖出(--gs-stop 绿) ▼ 标在上 —— 买红卖绿铁律, 位置为准。
    //   实心大号=已确认(收盘定死); 小号+○ 前缀=待确认(盘中价, 收盘才定死, 防疑似当确认)。
    //   **日线口径门控**(与 trendLine/niuxiong 同约定): gs_signals 是日线口径, 非日线周期不画(不给错口径的点)。
    if (showSignal && interval === '1d') {
      const gs = readGsColors()
      for (const g of effGsSignals) {
        const isBuy = g.side === 'G'
        markers.push({
          time: toChartTime(g.date, interval),
          position: isBuy ? ('belowBar' as const) : ('aboveBar' as const),
          color: gsColorFor(g.side, gs),
          shape: isBuy ? ('arrowUp' as const) : ('arrowDown' as const),
          size: g.confirmed ? 2 : 1,
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
    // K线形态标注(2026-10-10): 严格规则形态 → 记号(买红卖绿, 证据非建议; 受 pattern 图层开关)。
    // 同 (交易日+方向) 的多个形态合成为一个记号(见 lib/kline-patterns), 防完全重合。
    if (props.layersVisible?.pattern !== false) {
      const pc = readStockColors()
      markers.push(
        ...(buildPatternMarkers(effPatterns, (d) => toChartTime(d, interval), {
          up: pc.up,
          down: pc.down,
          neutral: readChartTheme().neutral,
        }) as never[]),
      )
    }
    // 威科夫量价结构标注(本土三层滤网 P1): 严格规则结构 → 记号(与形态同配色语义,
    // 看涨红/下方·看跌绿/上方·中性灰/下方, 证据非建议; 受 wyckoff 图层开关, 独立序列
    // 避免与形态记号 '/' 混连, 同根同向重合时两记号叠放——结构名不同, 悬停可区分)。
    if (props.layersVisible?.wyckoff !== false) {
      const pc = readStockColors()
      markers.push(
        ...(buildPatternMarkers(effWyckoff, (d) => toChartTime(d, interval), {
          up: pc.up,
          down: pc.down,
          neutral: readChartTheme().neutral,
        }) as never[]),
      )
    }
    // 决策先锋辅助指标买卖点(2026-10-10): 趋势操盘线买/卖点 + 牛熊线金叉/死叉 B/S(买红卖绿)。
    // 记号定位按信号自带日期 → 交给 filterMarkersInBarsRange 裁掉"落在K线区间外"的点(不给假定位)。
    // 受 `layersVisible.{trendLine,niuxiong}` 门控, 与形态标注共存(各自独立开关/图例)。
    {
      const gcol = readGsColors()
      const pc = { buy: gcol.go, sell: gcol.stop }
      if (props.layersVisible?.trendLine !== false && effTrendLine?.available) {
        markers.push(
          ...(trendSignalMarkers(effTrendLine, (d) => toChartTime(d, interval), pc) as never[]),
        )
      }
      if (props.layersVisible?.niuxiong !== false && effNiuxiong?.available) {
        markers.push(
          ...(niuxiongSignalMarkers(effNiuxiong, (d) => toChartTime(d, interval), pc) as never[]),
        )
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
    // AI/用户标注：有时间点的画 marker，有价位的画虚线（两类可同时存在）。
    if (props.layersVisible?.aiNote !== false && aiNotes.length) {
      const pc2 = readStockColors()
      for (const n of aiNotes) {
        if (!n.ts) continue
        markers.push({
          time: toChartTime(n.ts, interval),
          position: 'aboveBar' as const,
          color: n.color === 'down' ? pc2.down : pc2.up,
          shape: 'circle' as const,
          size: 1,
          text: `🤖 ${n.label}`,
        } as never)
      }
    }
    // LWC v5: 时间落在首/末根 K 线之外的 marker 会让 setMarkers 抛 "Value is null"
    // → 整页进错误边界(周末/节假日"当天有公告、当天没 K 线"必踩)。裁掉, 不假装定位。
    const safeMarkers = filterMarkersInBarsRange(markers, rawKlinesRef.current.map(b => b.time))
    try {
      if (!markerPluginRef.current) {
        markerPluginRef.current = createSeriesMarkers(series, safeMarkers)
      } else {
        markerPluginRef.current.setMarkers(safeMarkers)
      }
    } catch (e) {
      console.warn('[KlineChart] createSeriesMarkers failed, fallback to recreate:', e)
      try {
        markerPluginRef.current = createSeriesMarkers(series, safeMarkers)
      } catch (err) {
        console.error('[KlineChart] recreate marker failed:', err)
      }
    }

    // 2a) GS 区间色带(规格 §1, 2026-10-11): 由**已确认** G/S 信号逐根推区间 → 主图顶部短柱带
    //     (G区紫/S区绿)。末根 pending(未确认)不据它染色(防末根假交叉染错整段区间);
    //     首个已确认信号之前的 K 线无区间(不画)。日线口径门控, 整层 signal 关 → 清空。
    const zoneSeries = gsZoneSeriesRef.current
    if (zoneSeries) {
      if (showSignal && interval === '1d' && effGsSignals.length > 0) {
        const zonePts = gsZonePoints(effGsSignals, rangeBarsRef.current.map((b) => b.date))
        zoneSeries.setData(
          zonePts.map((z) => ({
            time: toChartTime(z.date, interval),
            value: 1,
            color: GS_ZONE_COLOR[z.zone],
          })) as never,
        )
      } else {
        zoneSeries.setData([])
      }
    }

    // 2) 支撑压力位 (L2 买卖点/价位线, showSignal 开关 + priceLinesVisible 过滤)。
    //    先清掉上一轮的价位线, 再按当前开关重建 — 避免切开关导致虚线累积。
    for (const line of priceLinesRef.current) series.removePriceLine(line)
    priceLinesRef.current = []
    // 2.5) AI 标注价位线（虚线 + 标题带 🤖，与人工支撑压力线区分）。
    if (props.layersVisible?.aiNote !== false) {
      for (const n of aiNotes) {
        if (n.price == null) continue
        priceLinesRef.current.push(
          series.createPriceLine({
            price: Number(n.price),
            color: readChartTheme().neutral,
            lineWidth: 1,
            lineStyle: 3, // dotted
            axisLabelVisible: true,
            title: `🤖 ${n.label}`.slice(0, 24),
          }),
        )
      }
    }

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
      for (const line of intentPriceLinesFor(intent, effAccumulationChips)) {
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

    // 2c) 情景关键位水平线(2026-10-22): 阻力/支撑/走强目标/走弱目标。
    //     受情景图层开关门控(**默认关**); 先清后建 ⇒ 切标的/关图层都不残留。
    //     买红卖绿: 阻力+走强目标=红系(涨), 支撑+走弱目标=绿系(跌)。
    for (const line of scenarioLinesRef.current) {
      try { series.removePriceLine(line) } catch { /* noop */ }
    }
    scenarioLinesRef.current = []
    if (showScenarioLayer) {
      for (const l of effScenarioLines) {
        if (!Number.isFinite(l.price)) continue
        scenarioLinesRef.current.push(
          series.createPriceLine({
            price: l.price,
            color: scenarioLineColor(l.color),
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: l.label,
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
        volSeries.setData(ascendSeries(histData, 'volume'))
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
      } else if (subchart === 'dark_pool') {
        // 暗盘资金档独占副图 pane(2026-10-11 规格 §2): 成交量让位, 免与暗盘柱重叠
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
          // 2026-10-24 P0: 资金柱数据来自 props(可能含回填/多源拼接), 同样不保证升序 ——
          // lightweight-charts 遇乱序直接抛断言并把整页打进 ErrorBoundary(实测 index=120)。
          const fundPts = effFundFlow
            .map((bar) => fundBarPoint(bar, interval, sc, theme.nodata))
            .filter((p) => {
              const t = Number(p.time as unknown as number)
              return Number.isFinite(t) && t > 0
            })
            .sort((a, b) => Number(a.time as unknown as number) - Number(b.time as unknown as number))
          if (fundPts.length !== effFundFlow.length) {
            console.warn('[KlineChart] fundFlow 含非法/重复时间点, 已过滤排序', { raw: effFundFlow.length, kept: fundPts.length })
          }
          fs.setData(fundPts)
          fs.priceScale().applyOptions({ visible: true } as never)
        } else {
          fs.setData([])
          fs.priceScale().applyOptions({ visible: false } as never)
        }
      }
      // 4) 暗盘资金柱(2026-10-11 规格 §2, subchart==='dark_pool'): 红柱=流入/绿柱=流出。
      //    0/脏值不画(`darkPoolBarPoint` 返回 null → 过滤, 绝不把 0 冒充成"平衡柱")。
      const dpr = darkPoolSeriesRef.current
      if (dpr) {
        if (subchart === 'dark_pool' && effDarkPool.length > 0) {
          const pts = effDarkPool
            .map((p) => darkPoolBarPoint(p, interval, sc))
            .filter((x): x is { time: Time; value: number; color: string } => x !== null)
            // 2026-10-24 P0: 同 fundFlow —— 时间必须为正且升序, 否则 lightweight-charts 断言抛错
            .filter((p) => {
              const t = Number(p.time as unknown as number)
              return Number.isFinite(t) && t > 0
            })
            .sort((a, b) => Number(a.time as unknown as number) - Number(b.time as unknown as number))
          dpr.setData(pts as never)
        } else {
          dpr.setData([])
        }
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effEvents, effPriceLines, props.costLines, effFundFlow, effActivitySeries, subchart, props.kindsVisible, props.priceLinesVisible, props.layersVisible, effGsSignals, effPatterns, effWyckoff, effTrendLine, effNiuxiong, effDarkPool, effScenarioLines, showScenarioLayer, interval, props.sourceReady, props.tradeMarkers, intent, aiNotes])

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

  // ── 决策先锋辅助指标三线图层(2026-10-10): 趋势操盘线(红/黄/绿) + 牛熊线(牛/马/买卖) ──
  // 全序列由前端按 API 回传 `params` 在**日线收盘价**上展开(与后端 EMA/WMA/SMA **同源公式** —— 后端
  // 每只只回最后一根快照, 全序列需展开; 与既有 MA5/10/20/60 的"前端自算"同策略)。日线以外周期口径不一致
  // (后端基于日K) → 仅 `interval==='1d'` 画线(诚实: 不给错口径的线)。门控与其它图层一致, 与形态共存。
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    for (const s of trendLineSeriesRef.current) { try { chart.removeSeries(s) } catch { /* noop */ } }
    trendLineSeriesRef.current = []
    for (const s of niuxiongLineSeriesRef.current) { try { chart.removeSeries(s) } catch { /* noop */ } }
    niuxiongLineSeriesRef.current = []
    const kl = rawKlinesRef.current
    if (interval !== '1d' || kl.length === 0) return
    const closes = kl.map((k) => k.close)
    const drawLine = (target: Array<ISeriesApi<'Line'>>, v: Array<number | null>, color: string) => {
      const line = chart.addSeries(LineSeries, { color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false })
      const pts = kl
        .map((k, i) => (v[i] == null ? null : { time: k.time, value: v[i] as number }))
        .filter((p): p is { time: Time; value: number } => p != null)
      line.setData(pts as never)
      target.push(line)
    }
    const gcol = readGsColors()
    if (props.layersVisible?.trendLine !== false && effTrendLine?.available && effTrendLine.lines) {
      const s = trendLineSeries(closes, effTrendLine.params)
      // 红=快线 / 黄=中线(专业图表语义色, 无令牌) / 绿=慢线
      drawLine(trendLineSeriesRef.current, s.red, gcol.go)
      drawLine(trendLineSeriesRef.current, s.yellow, 'rgba(234, 179, 8, 0.95)')
      drawLine(trendLineSeriesRef.current, s.green, gcol.stop)
    }
    if (props.layersVisible?.niuxiong !== false && effNiuxiong?.available && effNiuxiong.lines) {
      const s = niuxiongSeries(closes, effNiuxiong.params)
      // 牛=蓝 / 马=橙(与既有牛马线同色) / 买卖线=中性
      drawLine(niuxiongLineSeriesRef.current, s.bull, 'rgba(59, 130, 246, 0.95)')
      drawLine(niuxiongLineSeriesRef.current, s.horse, 'rgba(249, 115, 22, 0.95)')
      drawLine(niuxiongLineSeriesRef.current, s.trade, maShade(0.55))
    }
  }, [effTrendLine, effNiuxiong, interval, props.layersVisible?.trendLine, props.layersVisible?.niuxiong, rawKlinesRef.current.length])

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

  /**
   * lightweight-charts 对 setData 的硬约束：时间必须为正、升序、无重复，违反即抛断言
   * 并把整页打进 ErrorBoundary（2026-10-24 P0：/stocks/600519 实测 index=117 重复时间）。
   * 各 series 的数据来自不同源（K 线接口 / props 资金流 / 暗盘 / 形态），顺序都不可信，
   * 统一在这里收敛，避免逐个 setData 漏掉。
   */
  function ascendSeries<T extends { time: Time }>(pts: T[], tag: string): T[] {
    const timeOf = (p: T) => Number(p.time as unknown as number)
    const clean = pts.filter((p) => {
      const t = timeOf(p)
      return Number.isFinite(t) && t > 0
    })
    clean.sort((a, b) => timeOf(a) - timeOf(b))
    const dedup = clean.filter((p, i) => i === 0 || timeOf(p) !== timeOf(clean[i - 1]))
    if (dedup.length !== pts.length) {
      console.warn(`[KlineChart] ${tag} 时间乱序/重复/非法, 已收敛`, { raw: pts.length, kept: dedup.length })
    }
    return dedup
  }

  // G/S(含交割单买卖方向)的颜色规则验收钩子: K 线 marker 画在 canvas 上, DOM 里查不到 ——
  // 所以把**解析后的实际颜色**挂到容器上, 生产巡检(terminal_audit)据此断言
  // "G=红系 / S=绿系" 没被改反(改 CSS token 也拦得住)。
  const gsResolved = readGsColors()
  // 暗盘资金档悬停"四组合用法"(规格 §2): 收盘涨跌 × 暗盘红绿 → 用法提示(证据口径, 非建议)
  const hoverPoolCombo =
    subchart === 'dark_pool' && hoverReadout
      ? darkPoolCombo(hoverReadout.poolChg ?? null, hoverReadout.poolNet ?? null)
      : null

  return (
    <div
      className="flex flex-col gap-2"
      data-gs-go={gsResolved.go}
      data-gs-stop={gsResolved.stop}
      data-gs-count={effGsSignals.length}
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
            className={`min-h-[28px] px-2.5 py-1 text-xs rounded ${
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
              className={`min-h-[28px] px-2.5 py-1 text-xs rounded disabled:opacity-40 disabled:cursor-not-allowed ${
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
            className={`min-h-[28px] px-2.5 py-1 text-xs rounded ${
              mode === 'minute'
                ? 'bg-primary text-primary-foreground'
                : 'bg-secondary text-secondary-foreground hover:bg-secondary/80'
            }`}
          >
            分时
          </button>
        )}
        <button
          type="button"
          onClick={() => {
            setChipProfileOpen((v) => {
              const next = !v
              try { localStorage.setItem('sida_chip_profile', next ? '1' : '0') } catch { /* ignore */ }
              return next
            })
          }}
          className={`min-h-[28px] px-2.5 py-1 text-xs rounded font-medium transition-colors ${
            chipProfileOpen
              ? 'bg-primary text-primary-foreground'
              : 'bg-secondary text-secondary-foreground hover:bg-secondary/80'
          }`}
          title="在K线右侧展示/隐藏 120日主力波段筹码分布峰(CYQ)"
        >
          📊 筹码分布
        </button>
      </div>
      {/* 图表容器与右侧筹码分布图 */}
      <div className="relative w-full">
        <div
          ref={containerRef}
          className={mode === 'minute' && props.enableMinute ? 'hidden' : 'w-full'}
          style={{ minHeight: props.height ?? 360 }}
        />
        {/* K线右侧横向筹码分布山形图 (CYQ 直方图) */}
        {chipProfileOpen && mode === 'kline' && effAccumulationChips && effAccumulationChips.prices && effAccumulationChips.prices.length > 0 && (
          <div
            data-testid="chip-profile-histogram"
            className="absolute top-4 right-16 bottom-28 w-36 pointer-events-none flex flex-col justify-between z-20"
            title={`120日主力筹码沉淀分布 · 主峰: ${effAccumulationChips.main_cost} 元`}
          >
            {(() => {
              const prices = effAccumulationChips.prices as number[]
              const chips = effAccumulationChips.chips as number[]
              const maxChip = Math.max(...chips, 0.001)
              const lastClose = effAccumulationChips.last_close || 35.0
              // 挑选 28 个采样切片自顶向下排列展示
              const step = Math.max(1, Math.floor(prices.length / 28))
              const sampledIndices = []
              for (let i = prices.length - 1; i >= 0; i -= step) {
                sampledIndices.push(i)
              }
              return (
                <div className="h-full flex flex-col justify-between py-1 bg-background/80 dark:bg-black/60 backdrop-blur-md rounded p-1.5 border border-border/60 shadow-float pointer-events-auto">
                  <div className="flex items-center justify-between text-[10px] text-muted-foreground pb-1 border-b border-border/40 select-none">
                    <span className="font-medium">筹码分布 (CYQ)</span>
                    <span className="font-mono text-amber-500 font-bold">¥{safeFixed(effAccumulationChips.main_cost, 2)}</span>
                  </div>
                  <div className="flex-1 flex flex-col justify-between py-1">
                    {sampledIndices.map((idx) => {
                      const p = prices[idx]
                      const c = chips[idx]
                      const widthPct = Math.min(100, Math.round((c / maxChip) * 100))
                      const isProfit = p <= lastClose
                      const isPeak = Math.abs(p - (effAccumulationChips.main_cost || 0)) <= 0.25
                      return (
                        <div key={p} className="flex items-center justify-end h-1.5 gap-1" title={`¥${safeFixed(p, 2)}: 占比 ${widthPct}%`}>
                          <span className="font-mono text-[10px] text-muted-foreground/60 w-8 text-right select-none leading-none">
                            {safeFixed(p, 1)}
                          </span>
                          {isPeak && <span className="text-[10px] text-amber-500 font-bold leading-none select-none">◀峰</span>}
                          <div
                            className={`h-1.5 rounded-sm shadow-sm transition-all ${
                              isPeak
                                ? 'bg-amber-400 dark:bg-amber-500 shadow-amber-500/50'
                                : isProfit
                                ? 'bg-rose-500/85 dark:bg-rose-500'
                                : 'bg-emerald-500/85 dark:bg-emerald-400'
                            }`}
                            style={{ width: `${Math.max(8, widthPct)}%` }}
                          />
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })()}
          </div>
        )}
      </div>
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
          {/* 2026-10-12: 合成的今日 bar 未定型(分钟聚合/实时日线 forming, 18:00 才批量定型)。
              诚实小字: 一字符徽章/小字不卡片化 —— 只加一段 muted 小字, 悬停/图例可读。 */}
          {liveBar?.partial && (
            <span data-testid="kline-live-partial" className="ml-2 text-muted-foreground">
              盘中未定型（{liveBar.source || '实时合成'}
              {liveBar.as_of ? ` 截至 ${liveBar.as_of.replace('T', ' ').slice(0, 16)}` : ''}）
            </span>
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
          {/* 图表说明(2026-10-14): 当前 GS 标记「为什么」—— 悬停看触发/时点/失效(与推送/战报同源) */}
          {effZoneNote && (
            <span
              data-testid="gs-legend-why"
              className="text-[10px] cursor-help"
              title={markerWhy(effZoneNote)}
            >
              为什么? (悬停: {effZoneNote.label})
            </span>
          )}
        </div>
      )}

      {/* K线形态图例(2026-10-10): 识别到的形态客观罗列(悬停 title 看定义), 明确"证据非建议"。
          样式复用 gs-legend 的令牌色(买红卖绿), 不新增字号/背景, 不改布局密度。 */}
      {effPatterns.length > 0 && (
        <div
          data-testid="pattern-legend"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
        >
          <span className="font-medium text-foreground/80">K线形态</span>
          {effPatterns.map((m, i) => {
            // 图表说明(2026-10-14): 图例项挂「为什么」(触发/时点/失效) —— 与图上标的同源
            const why = noteForMarker(effChartNotes, 'pattern', m.date, m.name)
            const tip = [m.definition, why ? markerWhy(why) : ''].filter(Boolean).join('\n')
            return (
              <span key={`${m.name}-${m.index}-${i}`} title={tip}>
                <span className={m.direction === 'bearish' ? 'text-[hsl(var(--gs-stop))]' : 'text-[hsl(var(--gs-go))]'}>
                  {m.name}
                </span>
                <span className="ml-1">
                  {PATTERN_DIRECTION_LABEL[m.direction]}
                  {m.date ? ` ${m.date.slice(0, 10)}` : ''}
                  {m.position ? ` · ${m.position}` : ''}
                </span>
              </span>
            )
          })}
          <span className="text-[10px]">客观标注 · 非投资建议</span>
        </div>
      )}

      {/* 情景关键位图例(2026-10-22): 与形态/威科夫图例**同位置同风格**(一行式小字, 非卡片)。
          图上元素 = 水平线(阻力/支撑/走强目标/走弱目标), 这里只做"颜色=语义"映射 + 图层开关。
          开关默认**关**(不干扰原有视图), 父传 `layersVisible.scenario` 时以父为准。
          诚实口径: 关键位由均线/筹码带/区间高低算出, 非主观画线, 也不预测方向。 */}
      {showScenarioLayer && effScenarioLines.length > 0 && (
        <div
          data-testid="scenario-legend"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
        >
          <span className="font-medium text-foreground/80">情景关键位</span>
          {effScenarioLines.map((l) => (
            <span key={`${l.kind}-${l.price}`} className="flex items-center gap-x-1">
              <span
                aria-hidden="true"
                className={`inline-block h-px w-3 border-t border-dashed ${scenarioLineToneClass(l.color)}`}
              />
              <span className={scenarioLineToneClass(l.color)}>{l.label}</span>
              <span className="font-mono">{safeFixed(l.price, 2)}</span>
            </span>
          ))}
          <span className="text-[10px]">条件-反应 · 非方向预测</span>
        </div>
      )}
      {!showScenarioLayer && props.layersVisible?.scenario === undefined && (
        <div
          data-testid="scenario-layer-toggle"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
        >
          <button
            type="button"
            onClick={() => setScenarioLocalOn((v) => !v)}
            className="px-2 py-1 text-xs rounded bg-secondary text-secondary-foreground hover:bg-secondary/80"
            title="显示/隐藏情景关键位(阻力/支撑/走强目标/走弱目标)—— 后端情景矩阵算出的关键位, 不预测方向"
          >
            显示情景关键位
          </button>
          <span className="text-[10px]">阻力/支撑/目标位 · 条件-反应 · 非投资建议</span>
        </div>
      )}

      {/* 数智决策辅助指标图例(2026-10-10): 与形态图例**并列共存**, 各自分组(不互踩)。
          只放"颜色=语义"映射(现值读数在数据面板, 不在图例重复一行数字); 明确"客观标注 · 非投资建议"。 */}
      {props.layersVisible?.trendLine !== false && effTrendLine?.available && effTrendLine.lines && (
        <div
          data-testid="trend-line-legend"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
        >
          <span className="font-medium text-foreground/80">操盘线</span>
          <span>
            <span className="text-[hsl(var(--gs-go))]">红</span> 快线
          </span>
          <span>
            <span className="text-[#eab308]">黄</span> 中线
          </span>
          <span>
            <span className="text-[hsl(var(--gs-stop))]">绿</span> 慢线
          </span>
          {effTrendLine.trend ? <span>{effTrendLine.trend}</span> : null}
          {effTrendLine.band?.state ? <span>{effTrendLine.band.state}</span> : null}
          <span>买▲红 / 卖▼绿</span>
          <span className="text-[10px]">{PIONEER_NOT_ADVICE_TEXT}</span>
        </div>
      )}
      {props.layersVisible?.niuxiong !== false && effNiuxiong?.available && effNiuxiong.lines && (
        <div
          data-testid="niuxiong-legend"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
        >
          <span className="font-medium text-foreground/80">牛熊线</span>
          <span>
            <span className="text-[#3b82f6]">牛</span> 加权线
          </span>
          <span>
            <span className="text-[#f97316]">马</span> 均线
          </span>
          <span>买卖线</span>
          {effNiuxiong.signal ? (
            <span
              className={
                effNiuxiong.signal === 'B'
                  ? 'text-[hsl(var(--gs-go))]'
                  : 'text-[hsl(var(--gs-stop))]'
              }
            >
              {effNiuxiong.signal === 'B' ? '金叉 B（买）' : '死叉 S（卖）'}
            </span>
          ) : (
            <span>近期无交叉</span>
          )}
          <span className="text-[10px]">{PIONEER_NOT_ADVICE_TEXT}</span>
        </div>
      )}

      {/* GS 区间色带图例(2026-10-11 规格 §1): G区紫/S区绿短柱, 与买卖点标记**并列共存**(不互踩)。
          只在日线且有已确认信号时出现(与色带本身同门控, 无数据不冒领)。 */}
      {interval === '1d' && props.layersVisible?.signal !== false && effGsSignals.length > 0 && (
        <div
          data-testid="gs-zone-legend"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
        >
          <span className="font-medium text-foreground/80">GS 区间</span>
          <span>
            <span style={{ color: 'rgb(168, 85, 247)' }}>G区</span> 上方紫柱(趋势偏多)
          </span>
          <span>
            <span className="text-[hsl(var(--gs-stop))]">S区</span> 绿柱(趋势偏空)
          </span>
          <span className="text-[10px]">非函数指标 · 信号后不消失直至反向 · 客观标注</span>
        </div>
      )}

      {/* 暗盘资金副图图例(2026-10-11 规格 §2): 红柱=流入/绿柱=流出, 显式标 L1_approx(诚实近似),
          四组合用法在悬停读数里体现。仅该档激活时出现。 */}
      {subchart === 'dark_pool' && (
        <div
          data-testid="dark-pool-legend"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground"
        >
          <span className="font-medium text-foreground/80">暗盘资金</span>
          <span>
            <span className="text-[hsl(var(--gs-go))]">红柱</span> 流入
          </span>
          <span>
            <span className="text-[hsl(var(--gs-stop))]">绿柱</span> 流出
          </span>
          <span className="text-[10px]">历史日=OHLC分摊近似 · {DARK_POOL_CONFIDENCE}</span>
          <span className="text-[10px]">涨+红持股 / 跌+绿回避 / 跌+红吸筹 / 涨+绿诱多</span>
          <span className="text-[10px]">客观标注 · 非投资建议</span>
        </div>
      )}

      {/* 分时突破「突/积」提示条(2026-10-10): 后端缺逐分钟 DDE 时显式降级(available=false),
          前端**照实**展示降级文案, 不编造信号(证据非建议)。 */}
      {minuteBreakthrough && (
        <div
          data-testid="minute-breakthrough-bar"
          className={`mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] ${
            minuteBreakthrough.available && minuteBreakthrough.signalType
              ? 'text-foreground'
              : 'text-muted-foreground'
          }`}
        >
          <span className="font-medium text-foreground/80">分时突破</span>
          {minuteBreakthrough.available && minuteBreakthrough.signalType ? (
            <>
              <span
                className={`font-mono ${
                  minuteBreakthrough.signalType === '突'
                    ? 'text-[hsl(var(--gs-go))]'
                    : 'text-[hsl(var(--gs-stop))]'
                }`}
              >
                「{minuteBreakthrough.signalType}」
              </span>
              {minuteBreakthrough.triggerTime ? <span>{minuteBreakthrough.triggerTime}</span> : null}
              {minuteBreakthrough.metConditions.length > 0 ? (
                <span className="text-muted-foreground">
                  {minuteBreakthrough.metConditions.join(' · ')}
                </span>
              ) : null}
              <span className="text-[10px]">{PIONEER_NOT_ADVICE_TEXT}</span>
            </>
          ) : minuteBreakthrough.available ? (
            <span>暂未触发「突/积」</span>
          ) : (
            <>
              <span
                title={
                  minuteBreakthrough.note
                    ? `${minuteBreakthrough.note}\n${MINUTE_DDE_HINT}`
                    : MINUTE_DDE_HINT
                }
                data-testid="minute-dde-degrade-hint"
                className="cursor-help underline decoration-dotted decoration-muted-foreground/40 underline-offset-2"
              >
                {minuteDegradeText(minuteBreakthrough.reasons)}
              </span>
              {minuteBreakthrough.reasons.length > 0 ? (
                <span className="text-[10px]">（{minuteBreakthrough.reasons.join(' · ')}）</span>
              ) : null}
            </>
          )}
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
          {effAccumulationChips?.main_cost != null && (
            <span className="ml-2 text-rose-500 font-medium">
              底仓成本 <span className="font-mono">¥{safeFixed(effAccumulationChips.main_cost, 2)}</span>
              <span className="text-[10px] ml-1 text-muted-foreground">({effAccumulationChips.pattern})</span>
            </span>
          )}
        </div>
      )}

      {/* 图表说明条(2026-10-14 看图决策产品件): **一行式小字, 非卡片** ——
          逐标记「为什么在那」(触发/时点/失效)。AI 读的=图上标的(单一事实源);
          无说明不渲染(不留空壳)。悬停看全部(不撑破布局密度)。 */}
      {effChartNotes.length > 0 && (
        <div
          data-testid="chart-notes-bar"
          className="mt-1 text-[11px] text-muted-foreground"
          title={effChartNotes.map((n) => markerWhy(n)).join('\n')}
        >
          <span className="font-medium text-foreground/80">图表说明</span>
          <span className="ml-2">
            {effChartNotes.length} 个标记 · {notesBarText(effChartNotes, 3)}
          </span>
          {/* 当前 GS 标记说明(与推送/战报同源取句): 一行式小字 */}
          {effZoneNote && (
            <span className="ml-2" data-testid="chart-zone-note">
              GS: {zoneNoteText(effZoneNote)}
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
            {/* §2 暗盘资金档: 该根主力净额 + 四组合用法(规格 §2, 证据非建议) */}
            {subchart === 'dark_pool' && (
              <span>
                暗盘资金 {toAmount(hoverReadout.poolNet)}
                {hoverPoolCombo ? ` · ${hoverPoolCombo}` : ''}
              </span>
            )}
            {hoverReadout.events && hoverReadout.events.length > 0 && (
              <span className="text-foreground">{hoverReadout.events.join(' · ')}</span>
            )}
            {hoverReadout.patterns && hoverReadout.patterns.length > 0 && (
              <span className="text-foreground">形态 {hoverReadout.patterns.join(' · ')}</span>
            )}
            {/* 图表说明(2026-10-14): 该根命中标记的「为什么」(悬停浮层小字, 非卡片; 与图上标的同源) */}
            {hoverReadout.why && hoverReadout.why.length > 0 && (
              <span
                data-testid="chart-note-why"
                className="text-muted-foreground"
                title={hoverReadout.why.join('\n')}
              >
                为什么 {hoverReadout.why[0]}
              </span>
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