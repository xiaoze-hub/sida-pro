/**
 * 决策先锋辅助指标(趋势操盘线 / 牛熊线 / 分时突破)前端**纯逻辑**层。
 *
 * 数据源(后端已上线, 2026-10-10 P3 补差; fastapi 前缀 `/api`):
 *   - `GET /api/indicators/trend-line/{symbol}?market=CN`          趋势操盘线(红/黄/绿三线 + 买卖点)
 *   - `GET /api/indicators/niuxiong/{symbol}?market=CN`            牛熊线(牛/马/买卖线 + 金叉死叉 B/S)
 *   - `GET /api/indicators/minute-breakthrough/{symbol}?market=CN` 分时突破(突/积, 缺输入显式降级)
 *
 * 由 `KlineChart`(K 线图层)与 `PioneerIndicatorsSection`(数据面板节)消费; 本模块不 import 图表库,
 * 可独立单测(纯函数)。
 *
 * 铁律:
 *  - **信号是证据非建议**: 只客观枚举/标注, 文案不含"建议买入/卖出"等主观措辞;
 *  - **买红卖绿**: 买 / B = 红(`--gs-go`), 卖 / S = 绿(`--gs-stop`) —— 与 K 线既有买卖点同色语义;
 *  - **缺数据显式降级**: `available=false` 不画不给假值; 脏点进白名单过滤(缺字段/非法枚举/非有限数一律丢);
 *  - **三线全序列**: 后端每个指标只回**最后一根**快照(见 `src/core/trend_pilot_line.py` / `niuxiong_line.py`),
 *    全序列由本层按后端**同源公式**(EMA/WMA/SMA, 逐式对齐 python 实现)在**日线收盘价**上展开 ——
 *    与 KlineChart 既有 MA5/10/20/60"前端自算"同策略; 周期一律取 API 回传的 `params`, **不硬编码**。
 *    参数是官方未公开的**逆向近似**(后端 `calibration` 字段), 故图层/面板均带"待校准"标注。
 */

export type PioneerSide = 'buy' | 'sell'

/** 买卖点(证据): 客观规则 + 触发条件 + 触发时点/价。 */
export interface PioneerSignal {
  signal: PioneerSide
  rule?: string
  trigger?: string
  price?: number
  time?: string | null
}

export interface TrendLineData {
  available: boolean
  /** 后端降级说明(无数据时) */
  note?: string
  /** 最后一根 bar 的三线现值(后端快照) */
  lines: { red: number; yellow: number; green: number } | null
  band: { state?: string; low?: number; high?: number } | null
  trend?: string
  close?: number
  barTime?: string | null
  buyPoints: PioneerSignal[]
  sellPoints: PioneerSignal[]
  params: Record<string, number>
  calibration?: string
}

export interface NiuxiongData {
  available: boolean
  note?: string
  lines: { bull: number | null; horse: number | null; trade: number | null } | null
  signal: 'B' | 'S' | null
  color: 'red' | 'green' | null
  state?: string
  cross: { type: 'golden' | 'death' | null; direction: 'B' | 'S' | null; barsAgo: number | null; time: string | null } | null
  params: Record<string, number>
  calibration?: string
}

export interface MinuteCondition {
  name: string
  met: boolean
  detail?: string
}

export interface MinuteBreakthroughData {
  available: boolean
  signalType: '突' | '积' | null
  triggerTime: string | null
  conditions: MinuteCondition[]
  metConditions: string[]
  degraded: boolean
  reasons: string[]
  note?: string
  params: Record<string, number>
  calibration?: string
}

/** 分时突破降级文案(后端 `available=false` 时显式展示; 与后端"逐分钟 DDE 未接入"同义)。 */
export const MINUTE_DDE_DEGRADE_TEXT = '逐分钟 DDE 未接入 · 暂不出信号'
/** 客观标注免责(证据非建议)。 */
export const PIONEER_NOT_ADVICE_TEXT = '客观标注 · 非投资建议'

// ── 白名单清洗工具 ─────────────────────────────────────────────────────
const isObj = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v)
const str = (v: unknown): string => (typeof v === 'string' ? v : '')
const num = (v: unknown): number | undefined => {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v)
    if (Number.isFinite(n)) return n
  }
  return undefined
}
const numOr = (v: unknown, d: number | undefined): number | undefined => num(v) ?? d

function numRecord(v: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  if (!isObj(v)) return out
  for (const [k, val] of Object.entries(v)) {
    const n = num(val)
    if (n !== undefined) out[k] = n
  }
  return out
}

function normSignals(raw: unknown, side: PioneerSide): PioneerSignal[] {
  if (!Array.isArray(raw)) return []
  const out: PioneerSignal[] = []
  for (const item of raw) {
    if (!isObj(item)) continue
    out.push({
      signal: side,
      rule: str(item.rule) || undefined,
      trigger: str(item.trigger) || undefined,
      price: num(item.price),
      time: typeof item.time === 'string' ? item.time : null,
    })
  }
  return out
}

// ── 响应归一(白名单; 脏点不进图) ───────────────────────────────────────
export function normalizeTrendLine(raw: unknown): TrendLineData | null {
  if (!isObj(raw)) return null
  const linesRaw = isObj(raw.lines) ? raw.lines : null
  const red = linesRaw ? num(linesRaw.red) : undefined
  const yellow = linesRaw ? num(linesRaw.yellow) : undefined
  const green = linesRaw ? num(linesRaw.green) : undefined
  const lines =
    red !== undefined && yellow !== undefined && green !== undefined
      ? { red, yellow, green }
      : null
  const bandRaw = isObj(raw.band) ? raw.band : null
  const buyPoints = normSignals(raw.buy_points, 'buy')
  const sellPoints = normSignals(raw.sell_points, 'sell')
  return {
    available: raw.available === true,
    note: str(raw.note) || undefined,
    lines,
    band: bandRaw
      ? { state: str(bandRaw.state) || undefined, low: num(bandRaw.low), high: num(bandRaw.high) }
      : null,
    trend: str(raw.trend) || undefined,
    close: num(raw.close),
    barTime: typeof raw.bar_time === 'string' ? raw.bar_time : null,
    buyPoints,
    sellPoints,
    params: numRecord(raw.params),
    calibration: str(raw.calibration) || undefined,
  }
}

export function normalizeNiuxiong(raw: unknown): NiuxiongData | null {
  if (!isObj(raw)) return null
  const linesRaw = isObj(raw.lines) ? raw.lines : null
  const lines = linesRaw
    ? {
        bull: numOr(linesRaw.bull, undefined) ?? null,
        horse: numOr(linesRaw.horse, undefined) ?? null,
        trade: numOr(linesRaw.trade, undefined) ?? null,
      }
    : null
  const crossRaw = isObj(raw.cross) ? raw.cross : null
  const crossType = crossRaw && (crossRaw.type === 'golden' || crossRaw.type === 'death') ? crossRaw.type : null
  const crossDir = crossRaw && (crossRaw.direction === 'B' || crossRaw.direction === 'S') ? crossRaw.direction : null
  const sig = raw.signal === 'B' || raw.signal === 'S' ? raw.signal : null
  const col = raw.color === 'red' || raw.color === 'green' ? raw.color : null
  return {
    available: raw.available === true,
    note: str(raw.note) || undefined,
    lines,
    signal: sig,
    color: col,
    state: str(raw.state) || undefined,
    cross: crossRaw
      ? { type: crossType, direction: crossDir, barsAgo: numOr(crossRaw.bars_ago, undefined) ?? null, time: typeof crossRaw.time === 'string' ? crossRaw.time : null }
      : null,
    params: numRecord(raw.params),
    calibration: str(raw.calibration) || undefined,
  }
}

export function normalizeMinuteBreakthrough(raw: unknown): MinuteBreakthroughData | null {
  if (!isObj(raw)) return null
  const conditions: MinuteCondition[] = []
  if (Array.isArray(raw.conditions)) {
    for (const c of raw.conditions) {
      if (!isObj(c)) continue
      const name = str(c.name).trim()
      if (!name) continue
      conditions.push({ name, met: c.met === true, detail: str(c.detail) || undefined })
    }
  }
  const met = Array.isArray(raw.met_conditions)
    ? raw.met_conditions.filter((m): m is string => typeof m === 'string')
    : []
  const reasons = Array.isArray(raw.reasons) ? raw.reasons.filter((m): m is string => typeof m === 'string') : []
  const sig = raw.signal_type === '突' || raw.signal_type === '积' ? raw.signal_type : null
  return {
    available: raw.available === true,
    signalType: sig,
    triggerTime: typeof raw.trigger_time === 'string' ? raw.trigger_time : null,
    conditions,
    metConditions: met,
    degraded: raw.degraded === true,
    reasons,
    note: str(raw.note) || undefined,
    params: numRecord(raw.params),
    calibration: str(raw.calibration) || undefined,
  }
}

/**
 * 分时突破降级主文案: 缺"逐分钟 DDE"这一环(reasons 为空或含 DDE)时给**统一降级文案**;
 * 其它原因(如分钟数据缺失)原样透传后端 reasons —— 不把真实原因替换成泛化文案。
 */
export function minuteDegradeText(reasons: string[]): string {
  const dde = reasons.length === 0 || reasons.some((r) => /DDE/i.test(r))
  return dde ? MINUTE_DDE_DEGRADE_TEXT : '暂不出信号'
}

// ── 三线序列(与后端同源公式; 供 K 线图层展开全序列) ────────────────────
const DEFAULT_RED = 10
const DEFAULT_YELLOW = 20
const DEFAULT_GREEN = 60
const DEFAULT_BULL = 20
const DEFAULT_HORSE = 5
const DEFAULT_TRADE = 30

/** EMA(首值播种, k=2/(period+1)); 与 `src/core/trend_pilot_line.py::_ema_series` 逐式一致。 */
export function emaSeries(values: number[], period: number): Array<number | null> {
  const p = Math.floor(period)
  if (p <= 0 || values.length === 0) return values.map(() => null)
  const out: Array<number | null> = new Array(values.length).fill(null)
  const k = 2 / (p + 1)
  let prev = values[0]
  out[0] = prev
  for (let i = 1; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k)
    out[i] = prev
  }
  return out
}

/** SMA(简单均线); 前 period-1 位为 null(不足窗口不给值); 与 `niuxiong_line.py::sma_series` 同式。 */
export function smaSeries(values: number[], period: number): Array<number | null> {
  const p = Math.floor(period)
  if (p <= 0) return values.map(() => null)
  const out: Array<number | null> = new Array(values.length).fill(null)
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= p) sum -= values[i - p]
    if (i >= p - 1) out[i] = sum / p
  }
  return out
}

/** WMA(加权均线, 线性权重 1..period, 最近值权重最大); 前 period-1 位 null; 与 `niuxiong_line.py::wma_series` 同式。 */
export function wmaSeries(values: number[], period: number): Array<number | null> {
  const p = Math.floor(period)
  if (p <= 0) return []
  const denom = (p * (p + 1)) / 2
  const out: Array<number | null> = new Array(values.length).fill(null)
  for (let i = 0; i < values.length; i++) {
    if (i + 1 < p) continue
    let acc = 0
    for (let j = 0; j < p; j++) acc += (j + 1) * values[i - p + 1 + j]
    out[i] = acc / denom
  }
  return out
}

/** 趋势操盘线三线全序列(红/黄/绿 EMA); 周期取 API `params`(缺则按后端默认)。 */
export function trendLineSeries(
  closes: number[],
  params: Record<string, number>,
): { red: Array<number | null>; yellow: Array<number | null>; green: Array<number | null> } {
  const red = Math.floor(params.red_period ?? DEFAULT_RED)
  const yellow = Math.floor(params.yellow_period ?? DEFAULT_YELLOW)
  const green = Math.floor(params.green_period ?? DEFAULT_GREEN)
  return {
    red: emaSeries(closes, red),
    yellow: emaSeries(closes, yellow),
    green: emaSeries(closes, green),
  }
}

/** 牛熊线三线全序列(牛=WMA / 马=SMA / 买卖线=SMA); 周期取 API `params`。 */
export function niuxiongSeries(
  closes: number[],
  params: Record<string, number>,
): { bull: Array<number | null>; horse: Array<number | null>; trade: Array<number | null> } {
  const bull = Math.floor(params.bull_period ?? DEFAULT_BULL)
  const horse = Math.floor(params.horse_period ?? DEFAULT_HORSE)
  const trade = Math.floor(params.trade_period ?? DEFAULT_TRADE)
  return {
    bull: wmaSeries(closes, bull),
    horse: smaSeries(closes, horse),
    trade: smaSeries(closes, trade),
  }
}

// ── 信号 → 图表 marker 规格(买红卖绿) ─────────────────────────────────
export interface PioneerMarkerSpec<T> {
  time: T
  position: 'aboveBar' | 'belowBar'
  color: string
  shape: 'arrowUp' | 'arrowDown'
  text: string
}

export interface PioneerColors {
  /** 买 / B 方向色(红) */
  buy: string
  /** 卖 / S 方向色(绿) */
  sell: string
}

/** 'YYYY-MM-DD'(前 10 位须合法日)才可定位到 K 线。 */
function usableDate(date: string | null | undefined): string | null {
  const d = (date || '').slice(0, 10)
  return /^\d{4}-\d{2}-\d{2}$/.test(d) ? d : null
}

function sortByTime<T>(arr: PioneerMarkerSpec<T>[]): PioneerMarkerSpec<T>[] {
  return arr.sort((a, b) => {
    const ta: unknown = a.time
    const tb: unknown = b.time
    if (typeof ta === 'number' && typeof tb === 'number') return ta - tb
    const sa = String(ta)
    const sb = String(tb)
    return sa < sb ? -1 : sa > sb ? 1 : 0
  })
}

/**
 * 趋势操盘线买卖点 → 图表 marker。买=红/下方/arrowUp, 卖=绿/上方/arrowDown(买红卖绿)。
 * 无合法日期的点**不画**(不给假定位)。
 */
export function trendSignalMarkers<T>(
  data: TrendLineData,
  toTime: (date: string) => T,
  colors: PioneerColors,
): PioneerMarkerSpec<T>[] {
  const out: PioneerMarkerSpec<T>[] = []
  for (const s of data.buyPoints) {
    const d = usableDate(s.time)
    if (!d) continue
    out.push({ time: toTime(d), position: 'belowBar', color: colors.buy, shape: 'arrowUp', text: '买' })
  }
  for (const s of data.sellPoints) {
    const d = usableDate(s.time)
    if (!d) continue
    out.push({ time: toTime(d), position: 'aboveBar', color: colors.sell, shape: 'arrowDown', text: '卖' })
  }
  return sortByTime(out)
}

/**
 * 牛熊线金叉/死叉 → 图表 marker(B=红/下方/arrowUp, S=绿/上方/arrowDown)。
 * 时点取 `cross.time`; 无合法日期则不画。
 */
export function niuxiongSignalMarkers<T>(
  data: NiuxiongData,
  toTime: (date: string) => T,
  colors: PioneerColors,
): PioneerMarkerSpec<T>[] {
  const d = usableDate(data.cross?.time)
  if (!data.signal || !d) return []
  const isBuy = data.signal === 'B'
  return [
    {
      time: toTime(d),
      position: isBuy ? 'belowBar' : 'aboveBar',
      color: isBuy ? colors.buy : colors.sell,
      shape: isBuy ? 'arrowUp' : 'arrowDown',
      text: data.signal,
    },
  ]
}
