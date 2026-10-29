/**
 * 图表上下文（P2, 2026-10-29）：个股工作台 → 数智参谋的唯一数据通道。
 *
 * ## 为什么需要它
 * 用户在图上看的是「这只票、这个周期、这一段可视区间、图上这几条已确认价位」，进参谋追问时
 * 现在只带了代码 —— 模型只能重新猜一遍用户在看什么，或者干脆反问。带上这段上下文，
 * 「这个区间的量能怎么看」才有指代对象。
 *
 * ## 三条硬约束（本模块的全部存在理由）
 *  1. **只带可证明的**：周期只认页面 URL（`?period=`）、区间只认图表真实上报的读数
 *     （`KlineChart.onRangeStats`）、价位只认服务端已确认的标注行。任一字段拿不到就**不写**
 *     —— 不猜默认周期、不补 0、不截取"看起来还行"的半份读数。
 *  2. **用户主动提问才生效**：本模块只产出文本；真正送进模型的时机是用户第一次发问时
 *     `createConversation(initial_context)`。进入参谋本身不发请求、不自动分析。
 *  3. **不越权**：文本里明写"不得据此自动下单、自动画线/标注或修改图表"。本模块不写标注、
 *     不触发任何交易/图表接口，只做字符串。
 *
 * ## 隔离
 * 发布端按 `账号|类型|市场|代码` 存（同一 key 覆盖写）；读取端默认按当前账号过滤，
 * 且**同号多类型并存时返回 null**（000001 既是上证指数又是平安银行 —— 不猜）。
 * 换标的/换周期/离开页面由调用方 `unpublishChartContext`，仓库只存"当前这一张图"。
 */

import { safeFixed, safePercent } from '@/lib/format'

import { currentAccountId } from './annotationLedger'

/** 工作台能承载的标的类型（与 `workbench-tabs` 的 `WorkbenchType` 同义，此处不反向依赖）。 */
export type ChartInstrument = 'stock' | 'index' | 'board'
/** 工作台路由支持的市场（`?market=`）。 */
export type ChartMarket = 'CN' | 'HK' | 'US'

/** 图表可视区间读数（逐字来自 `KlineRangeStats` 的可证子集）。 */
export interface ChartRange {
  /** 起始 K 线日期 'YYYY-MM-DD' */
  from: string
  /** 结束 K 线日期 'YYYY-MM-DD' */
  to: string
  /** 区间内 K 线根数（>0） */
  bars: number
  /** 区间涨跌幅 % */
  changePct: number
  /** 区间振幅 % */
  amplitudePct: number
}

/** 图上已确认价位（逐字来自服务端标注行）。 */
export interface ChartLevel {
  price: number
  kind: 'support' | 'pressure'
  /** 服务端给的标签；空串会被丢掉 */
  label?: string
}

/** 一次「进入参谋时」的图表上下文快照。 */
export interface ChartContext {
  /** 账号（JWT sub）。读侧必须比对，防止别人的视图串进来 */
  owner: string
  symbol: string
  instrument: ChartInstrument
  market: ChartMarket
  /** 周期，只来自页面 URL；缺失/非法即 undefined（不猜默认周期） */
  period?: string
  /** 可视区间；拿不到完整读数即 undefined */
  range?: ChartRange
  /** 已确认价位；没有即 undefined */
  levels?: ChartLevel[]
  /** 捕获时刻（ms）。仅用于"进入时快照"的语义与仓库容量淘汰 */
  capturedAt: number
}

/** `buildChartContext` 的输入：全部按"可能拿不到"处理。 */
export interface ChartContextInput {
  owner?: string | null
  symbol?: string | null
  instrument?: string | null
  market?: string | null
  period?: string | null
  range?: {
    from?: unknown
    to?: unknown
    bars?: unknown
    changePct?: unknown
    amplitudePct?: unknown
  } | null
  levels?: Array<{ price?: unknown; kind?: unknown; label?: unknown }> | null
  /** 测试用固定时钟 */
  now?: number
}

/** 一条上下文最多带几条价位（prompt 不是标注清单，别整块灌进去）。 */
export const CHART_CONTEXT_MAX_LEVELS = 6
/** 仓库容量上限（keep-alive 页签可能同时留着多张图；超出按捕获时间淘汰最旧）。 */
const STORE_MAX = 8

/** `?period=` 取值 → 人话（与 `lib/kline-period` 的合法取值一致；`intra` 不在其中）。 */
export const PERIOD_LABELS: Readonly<Record<string, string>> = {
  m1: '1分钟',
  m5: '5分钟',
  m15: '15分钟',
  m30: '30分钟',
  m60: '60分钟',
  d1: '日线',
  w1: '周线',
  mn: '月线',
}

const INSTRUMENT_LABELS: Readonly<Record<ChartInstrument, string>> = {
  stock: '个股',
  index: '指数',
  board: '板块',
}

const SYMBOL_RE = /^[0-9A-Za-z][0-9A-Za-z.-]{0,15}$/
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const INSTRUMENTS: ReadonlyArray<ChartInstrument> = ['stock', 'index', 'board']
const MARKETS: ReadonlyArray<ChartMarket> = ['CN', 'HK', 'US']

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** 可视区间：任一字段不成立就整段放弃（不回填、不截取）。 */
function buildRange(raw: ChartContextInput['range']): ChartRange | undefined {
  if (!raw) return undefined
  const from = clean(raw.from)
  const to = clean(raw.to)
  if (!DATE_RE.test(from) || !DATE_RE.test(to) || from > to) return undefined
  const bars = finite(raw.bars)
  const changePct = finite(raw.changePct)
  const amplitudePct = finite(raw.amplitudePct)
  if (bars === null || !Number.isInteger(bars) || bars <= 0) return undefined
  if (changePct === null || amplitudePct === null) return undefined
  return { from, to, bars, changePct, amplitudePct }
}

/** 价位：只留正的有限数 + 合法方向；同价保留第一条；上限 `CHART_CONTEXT_MAX_LEVELS`。 */
function buildLevels(raw: ChartContextInput['levels']): ChartLevel[] | undefined {
  if (!Array.isArray(raw)) return undefined
  const out: ChartLevel[] = []
  const seen = new Set<number>()
  for (const row of raw) {
    if (!row) continue
    const price = finite(row.price)
    if (price === null || price <= 0) continue
    const kind = row.kind === 'support' || row.kind === 'pressure' ? row.kind : null
    if (!kind) continue
    if (seen.has(price)) continue
    seen.add(price)
    const label = clean(row.label)
    out.push(label ? { price, kind, label } : { price, kind })
    if (out.length >= CHART_CONTEXT_MAX_LEVELS) break
  }
  return out.length > 0 ? out : undefined
}

/**
 * 从工作台真实状态构造上下文。
 *
 * @returns 身份（账号/代码/类型/市场）任一不可证 → `null`（整段作废，绝不带半份）。
 *          周期/区间/价位各自独立，"拿不到"只是不写那一项。
 */
export function buildChartContext(input: ChartContextInput): ChartContext | null {
  const owner = clean(input.owner)
  const symbol = clean(input.symbol)
  const instrument = clean(input.instrument) as ChartInstrument
  const market = clean(input.market).toUpperCase() as ChartMarket
  if (!owner || !SYMBOL_RE.test(symbol)) return null
  if (!INSTRUMENTS.includes(instrument)) return null
  if (!MARKETS.includes(market)) return null

  const rawPeriod = clean(input.period).toLowerCase()
  const ctx: ChartContext = {
    owner,
    symbol,
    instrument,
    market,
    capturedAt: typeof input.now === 'number' && Number.isFinite(input.now) ? input.now : Date.now(),
  }
  // 周期只认 URL 上的合法取值：非法/缺失 = 用户没选（或链接被降级），不写。
  if (rawPeriod in PERIOD_LABELS) ctx.period = rawPeriod
  const range = buildRange(input.range)
  if (range) ctx.range = range
  const levels = buildLevels(input.levels)
  if (levels) ctx.levels = levels
  return ctx
}

/** 该上下文的仓库 key（账号|类型|市场|代码）。 */
export function chartContextKey(ctx: Pick<ChartContext, 'owner' | 'instrument' | 'market' | 'symbol'>): string {
  return `${ctx.owner}|${ctx.instrument}|${ctx.market}|${ctx.symbol}`
}

/**
 * 用户可见的上下文分段（会话顶部那条）。
 *
 * 只陈述有据的部分：没有区间就不出现"区间"，没有周期就不出现周期标签 —— 界面上不出现占位假值。
 */
export function chartContextParts(ctx: ChartContext): string[] {
  const parts = [`${INSTRUMENT_LABELS[ctx.instrument]} · ${ctx.market}`]
  if (ctx.period) parts.push(PERIOD_LABELS[ctx.period] ?? ctx.period)
  if (ctx.range) {
    const { from, to, bars, changePct } = ctx.range
    parts.push(`区间 ${from}→${to}（${bars} 根，${safePercent(changePct, 2)}）`)
  }
  if (ctx.levels && ctx.levels.length > 0) {
    parts.push(`价位 ${ctx.levels.map((l) => safeFixed(l.price, 2)).join(' / ')}`)
  }
  return parts
}

/** 单行摘要（title / 测试 / 日志用）。 */
export function chartContextSummary(ctx: ChartContext): string {
  return chartContextParts(ctx).join(' · ')
}

/**
 * 注入会话 `initial_context` 的文本。
 *
 * 措辞承担三件事：说明这是**进入时快照**、限定只在用户提问相关时才引用、
 * 明写不得自动下单/自动画线（上下文是"看到什么"，不是"要做什么"）。
 */
export function chartContextPrompt(ctx: ChartContext): string {
  const lines: string[] = [
    '[图表上下文] 用户从个股工作台进入参谋时，屏幕上那张图的真实读数（进入时快照，不是实时值）。',
    `标的：${ctx.symbol}（${INSTRUMENT_LABELS[ctx.instrument]} · ${ctx.market}）`,
  ]
  if (ctx.period) lines.push(`周期：${PERIOD_LABELS[ctx.period] ?? ctx.period}（来自页面 URL ?period=${ctx.period}）`)
  if (ctx.range) {
    const { from, to, bars, changePct, amplitudePct } = ctx.range
    lines.push(
      `可见区间：${from}→${to}，${bars} 根，涨跌 ${safePercent(changePct, 2)}，振幅 ${safePercent(amplitudePct, 2, false)}`,
    )
  }
  if (ctx.levels && ctx.levels.length > 0) {
    lines.push(
      `图上价位：${ctx.levels
        .map((l) => `${safeFixed(l.price, 2)}（${l.kind === 'support' ? '支撑' : '压力'}${l.label ? ` · ${l.label}` : ''}）`)
        .join(' · ')}`,
    )
  }
  lines.push(
    '使用纪律：',
    '1. 只在用户提问涉及这张图的区间/价位/形态时才引用以上读数；用户没问就**不要主动分析**，也不要复述这段上下文。',
    '2. 以上是进入时的快照；需要当前价格或最新数据必须调用工具重新取数，不得把快照当实时。',
    '3. 不得据此自动下单、自动画线/标注或修改图表：交易与标注都必须由用户明确确认。',
  )
  return lines.join('\n')
}

// ---------------------------------------------------------------------------
// 发布/读取仓库
//
// 为什么是模块级 Map 而不是 context/prop：入口胶囊（`CopilotCapsule`）与会话容器
// （`AssistantWorkspace`）之间只靠 `sida-open-chat` 事件通信，事件 detail 里没有图表读数；
// 而本次任务不改这两处，故由工作台把"当前这一张图"发布到仓库、参谋在消费入口时按
// **账号+代码+市场**精确读取。仓库只存数据，权限/属主校验在服务端（标注读取已由 JWT 过滤）。
// ---------------------------------------------------------------------------

const store = new Map<string, ChartContext>()

/** 发布/覆盖当前视图上下文。返回仓库 key（调用方换标的时用它取消发布）。 */
export function publishChartContext(ctx: ChartContext): string {
  const key = chartContextKey(ctx)
  store.set(key, ctx)
  if (store.size > STORE_MAX) {
    // 超容量按捕获时间淘汰最旧的（keep-alive 页签留下的旧图不该长期占位）
    const oldest = [...store.entries()].sort((a, b) => a[1].capturedAt - b[1].capturedAt)[0]
    if (oldest && oldest[0] !== key) store.delete(oldest[0])
  }
  return key
}

/** 取消发布（换标的/离开个股视图/卸载）。 */
export function unpublishChartContext(key: string): void {
  store.delete(key)
}

/**
 * 按 代码(+市场) 读取当前账号的上下文。
 *
 * - 默认按 `currentAccountId()` 过滤（读不到身份 → 一律 null，不猜）；
 * - 传 `market` 时按市场精确比对（同号不同市场不互相证明）；
 * - **同号多类型并存 → null**：`000001` 既是上证指数又是平安银行，宁可不带也不猜类型。
 */
export function readChartContext(symbol: string, market?: string | null, owner?: string | null): ChartContext | null {
  const sym = clean(symbol)
  if (!sym) return null
  const who = owner === undefined ? currentAccountId() : clean(owner)
  if (!who) return null
  const wantMarket = clean(market).toUpperCase()
  const hits = [...store.values()].filter(
    (ctx) => ctx.symbol === sym && ctx.owner === who && (!wantMarket || ctx.market === wantMarket),
  )
  if (hits.length === 1) return hits[0]
  return null
}

/** 测试/登出用：清空仓库（生产渲染路径不要调用）。 */
export function clearChartContextStore(): void {
  store.clear()
}

/** 当前仓库条目数（测试/诊断用）。 */
export function chartContextStoreSize(): number {
  return store.size
}
