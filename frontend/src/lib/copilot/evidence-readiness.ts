/**
 * 证据齐套度（2026-10-23，参谋 UX）。
 *
 * 背景：流式取数只有一条单调的「正在取数 3/10：get_technical_analysis」，用户看到的是
 * 一个**总数**，看不到「哪几个维度已经到手、哪个还在跑、哪个没取到」。回答写完这条线就消失，
 * 依据里的数字因此不可追溯。这里把同一条 stage 流折成**按维度**的齐套度状态，
 * 既能在流中做进度条，也能在回答定稿后留成一条可追溯的痕迹。
 *
 * 数据来源仍是既有 SSE 的 `stage` 事件（`src/harness/pipeline.py:run_stream`），
 * 不新增后端字段、不改契约：
 *   `正在取数 3/10：get_technical_analysis`
 *
 * 三条口径铁律：
 *  1. **分母不许编**：只有标签里真的带 `/N` 才能有 total；没有就是 null，UI 显示「已取 N 项」。
 *  2. **未知工具不许猜中文名**：查不到映射就原样显示工具名（`get_foo`），分组落「其他」。
 *     猜成「主力」比显示 `get_foo` 更糟 —— 用户会以为这个维度真的取到了。
 *  3. **失败要留痕**：事件带 error / ok:false 时状态是 failed，不被后续的「看起来还行」洗白。
 *
 * 已知的推断（写清楚，别当事实）：pipeline 只在**工具开跑时**投一条 stage，
 * 不投「跑完」。所以下一条 stage 到达时，把上一条的 pending 置 ok —— 依据是同一轮内工具按序推进。
 * 若某工具随后报错，error 事件会把它改回 failed（failed 是粘性的，除非显式 ok:true）。
 */

export type ReadinessStatus = 'pending' | 'ok' | 'failed'

/** 六个证据维度 + 「其他」（未知工具的唯一诚实去处，不硬塞进六个维度之一）。 */
export type ReadinessGroup = '走势' | '筹码' | '主力' | '盘面' | '消息' | '情景' | '其他'

export interface StageLabel {
  /** 第几条（1 基）。标签没有 `/N` 形式时为 null —— 不补 0、不补 1。 */
  index: number | null
  /** 本轮总数。标签没有 `/N` 时 null。 */
  total: number | null
  /** 原始工具名（未翻译）。 */
  tool: string
}

export interface ReadinessItem {
  /** 去重键（工具名）。同一工具重试仍占一条，不重复铺 pill。 */
  key: string
  label: string
  group: ReadinessGroup
  status: ReadinessStatus
}

export interface ReadinessState {
  items: ReadinessItem[]
  /** 已确认拿到（status=ok）的条数。 */
  done: number
  /** 后端报的总数；null 表示未知（UI 只显示已完成条数）。 */
  total: number | null
  failed: number
}

// ── 工具名 → 人话 + 维度 ────────────────────────────────────────────────────
// 只收产品里真实存在的工具（`src/web/api/chat.py:_TOOL_STAGE_LABELS` / 参谋配方）。
// 标签刻意短：一行要能并排 5~8 个 pill，长名会换行成两行。
const TOOL_META: Record<string, { label: string; group: ReadinessGroup }> = {
  // 走势
  get_stock_quote: { label: '行情', group: '走势' },
  get_technical_analysis: { label: '技术面', group: '走势' },
  get_kline_patterns: { label: 'K线形态', group: '走势' },
  get_wyckoff_structure: { label: '威科夫', group: '走势' },
  get_thsdk_market_data_index: { label: '指数', group: '走势' },
  // 筹码
  get_chip_accumulation: { label: '底仓筹码', group: '筹码' },
  get_cancel_order_intent: { label: '撤单', group: '筹码' },
  get_orderbook: { label: '盘口', group: '筹码' },
  // 主力
  get_main_intent: { label: '主力意图', group: '主力' },
  get_intent_explain: { label: '意图解读', group: '主力' },
  get_capital_flow: { label: '资金流', group: '主力' },
  get_delta_series: { label: '逐笔', group: '主力' },
  get_main_flow_compare: { label: '双源比对', group: '主力' },
  get_thsdk_dde: { label: 'DDE', group: '主力' },
  get_northbound: { label: '北向', group: '主力' },
  // 盘面
  get_auction_data: { label: '竞价', group: '盘面' },
  scan_auction_anomaly: { label: '竞价异动', group: '盘面' },
  get_sentiment_cycle: { label: '情绪周期', group: '盘面' },
  get_market_anomalies: { label: '异动池', group: '盘面' },
  get_hot_stocks: { label: '热榜', group: '盘面' },
  get_stock_sectors: { label: '板块', group: '盘面' },
  get_rally_analysis: { label: '拉升段', group: '盘面' },
  // 消息
  get_market_news: { label: '消息面', group: '消息' },
  get_thsdk_news: { label: '个股新闻', group: '消息' },
  get_irm_qa: { label: '互动易', group: '消息' },
  get_event_catalyst: { label: '事件催化', group: '消息' },
  get_web_content: { label: '网页', group: '消息' },
  // 情景
  get_forecast: { label: '系统预测', group: '情景' },
  get_opportunities: { label: '今日机会', group: '情景' },
  get_strategy_signals: { label: '策略信号', group: '情景' },
  get_decision_pioneer: { label: '决策三指标', group: '情景' },
  get_demon_factor: { label: '妖股因子', group: '情景' },
  get_stock_dossier: { label: '全景档案', group: '情景' },
  get_portfolio: { label: '持仓', group: '情景' },
}

/** 工具元信息。未收录的工具**原样返回工具名**，分组落「其他」（绝不猜中文维度）。 */
export function toolMeta(tool: string): { label: string; group: ReadinessGroup } {
  const key = (tool || '').trim()
  const hit = TOOL_META[key]
  if (hit) return hit
  return { label: key || '--', group: '其他' }
}

export function toolLabel(tool: string): string {
  return toolMeta(tool).label
}

export function toolGroup(tool: string): ReadinessGroup {
  return toolMeta(tool).group
}

// ── stage 标签解析 ─────────────────────────────────────────────────────────
// 宽容：前缀随意（「正在取数」「取数中」「fetch」）、冒号半/全角、数字与斜杠间空格。
// 认不出工具名就返回 null —— 宁可不显示 pill，也不要把一句散文当工具。
const TOOL_TOKEN = '[A-Za-z][A-Za-z0-9_]*(?:[.-][A-Za-z0-9_]+)*'
const RE_WITH_TOTAL = new RegExp(
  `(?<!\\d)(\\d{1,4})\\s*[/／]\\s*(\\d{1,4})(?!\\d)\\s*[:：]?\\s*(${TOOL_TOKEN})`,
)
const RE_AFTER_SEP = new RegExp(`[:：]\\s*(${TOOL_TOKEN})`)
const RE_AFTER_KEYWORD = new RegExp(`(?:取数|调取|fetch(?:ing)?)\\s*[:：]?\\s*(${TOOL_TOKEN})`, 'i')
const RE_BARE = new RegExp(`^\\s*(${TOOL_TOKEN})\\s*$`)

function asTool(token: string | undefined | null): string | null {
  const t = (token || '').trim()
  if (!t) return null
  // 纯数字不是工具；长度 1 的字母也别当工具（避免「A 股」之类误判）
  if (/^\d+$/.test(t)) return null
  if (t.length < 3) return null
  return t
}

/**
 * `正在取数 3/10：get_technical_analysis` → `{index:3,total:10,tool:'get_technical_analysis'}`。
 * 无 `/N` 时 index/total 皆为 null（诚实：不知道分母就不编）。
 * 纯中文散文（「正在准备上下文…」「正在获取技术面分析...」）返回 null。
 */
export function parseStageLabel(text: string | null | undefined): StageLabel | null {
  const s = (text || '').trim()
  if (!s) return null

  const withTotal = RE_WITH_TOTAL.exec(s)
  if (withTotal) {
    const index = Number(withTotal[1])
    const total = Number(withTotal[2])
    const tool = asTool(withTotal[3])
    if (tool) return { index: index > 0 ? index : null, total: total > 0 ? total : null, tool }
  }

  for (const re of [RE_AFTER_KEYWORD, RE_AFTER_SEP, RE_BARE]) {
    const m = re.exec(s)
    const tool = asTool(m?.[1])
    if (tool) return { index: null, total: null, tool }
  }
  return null
}

// ── 事件形状（后端只发字符串；对象形状是给 trace/error 的防御性入口） ──────
export interface StageEventObject {
  stage?: string
  label?: string
  message?: string
  text?: string
  tool?: string
  ok?: boolean
  status?: string
  error?: string | null
}

export type StageEvent = string | StageEventObject | null | undefined

export interface NormalizedStageEvent {
  tool: string
  label: string
  group: ReadinessGroup
  failed: boolean
  index: number | null
  total: number | null
}

const FAIL_TEXT_RE = /(失败|未取到|超时|不可用|error|failed)/i

function textOfObject(evt: StageEventObject): string {
  return (evt.stage || evt.label || evt.message || evt.text || '').trim()
}

function failedOfObject(evt: StageEventObject): boolean {
  if (evt.ok === false) return true
  if (typeof evt.error === 'string' && evt.error.trim()) return true
  const st = (evt.status || '').toLowerCase()
  if (st === 'failed' || st === 'error' || st === 'fail') return true
  return false
}

/** 把 stage/trace 事件归一成一条「某工具开跑/失败」。认不出工具 → null（不污染齐套度）。 */
export function normalizeStageEvent(event: StageEvent): NormalizedStageEvent | null {
  if (event == null) return null

  if (typeof event === 'string') {
    const parsed = parseStageLabel(event)
    if (!parsed) return null
    const meta = toolMeta(parsed.tool)
    return {
      tool: parsed.tool,
      label: meta.label,
      group: meta.group,
      failed: FAIL_TEXT_RE.test(event),
      index: parsed.index,
      total: parsed.total,
    }
  }

  const parsed = parseStageLabel(textOfObject(event))
  const explicitTool = asTool(event.tool)
  const tool = explicitTool || parsed?.tool || null
  if (!tool) return null
  const meta = toolMeta(tool)
  const failed = failedOfObject(event) || (typeof event.error === 'string' && !!event.error.trim())
  return {
    tool,
    label: meta.label,
    group: meta.group,
    failed,
    index: parsed?.index ?? null,
    total: parsed?.total ?? null,
  }
}

// ── reducer ────────────────────────────────────────────────────────────────

export function emptyReadiness(): ReadinessState {
  return { items: [], done: 0, total: null, failed: 0 }
}

function recount(items: ReadinessItem[], total: number | null): ReadinessState {
  let done = 0
  let failed = 0
  for (const it of items) {
    if (it.status === 'ok') done += 1
    else if (it.status === 'failed') failed += 1
  }
  return { items, done, total, failed }
}

/**
 * 折一条事件进去（纯函数，返回新 state）。
 *
 * 状态机：
 *  - 新工具 → 追加一条 pending（事件带 error / ok:false → failed）；
 *  - 已有工具 → 重置为 pending（重试开跑）；原来 failed 的保持 failed（粘性，除非显式 ok:true）；
 *  - 上一条还在 pending 的，在下一条开跑时置 ok（pipeline 只报开跑，不报完成 —— 见文件头）。
 */
export function foldStageEvent(state: ReadinessState, event: StageEvent): ReadinessState {
  const ev = normalizeStageEvent(event)
  if (!ev) return state

  const total = ev.total ?? state.total
  const items = state.items.map((it) => {
    if (it.key === ev.tool) {
      if (it.status === 'failed' && !ev.failed) return it
      return { ...it, status: ev.failed ? ('failed' as ReadinessStatus) : ('pending' as ReadinessStatus) }
    }
    // 上一轮在跑的 → 下一条开跑即视为已返回（不是新工具的条目不动）
    if (it.status === 'pending') return { ...it, status: 'ok' as ReadinessStatus }
    return it
  })

  if (!items.some((it) => it.key === ev.tool)) {
    items.push({
      key: ev.tool,
      label: ev.label,
      group: ev.group,
      status: ev.failed ? 'failed' : 'pending',
    })
  }

  return recount(items, total)
}

export function foldStageEvents(events: StageEvent[]): ReadinessState {
  let state = emptyReadiness()
  for (const ev of events) state = foldStageEvent(state, ev)
  return state
}

/** 流结束：还在 pending 的置 ok（回答已经写完，说明取数阶段结束了）。failed 不受影响。 */
export function settleReadiness(state: ReadinessState): ReadinessState {
  if (!state.items.length) return state
  const items = state.items.map((it) =>
    it.status === 'pending' ? { ...it, status: 'ok' as ReadinessStatus } : it,
  )
  return recount(items, state.total)
}

/** 便捷入口：一串 stage 文案 → 终态（流结束后用）。 */
export function readinessFromLabels(labels: string[]): ReadinessState {
  return settleReadiness(foldStageEvents(labels))
}

/**
 * 计数文案。分母未知时只说「已取 N 项」，**不写 N/-- 也不写 N/1**。
 */
export function readinessProgressLabel(state: Pick<ReadinessState, 'done' | 'total'>): string {
  if (state.total == null) return `已取 ${state.done} 项`
  return `已取 ${state.done}/${state.total}`
}

/** 单条状态的可见文案（✓ / ⏳ / 未取到）。failed 读「未取到」而不是「失败」—— 说人话，且不夸大。 */
export function statusText(status: ReadinessStatus): string {
  if (status === 'ok') return '✓'
  if (status === 'failed') return '未取到'
  return '⏳'
}
