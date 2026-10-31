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
 * 四条口径铁律：
 *  1. **分母不许编**：只有标签里真的带 `/N` 才能有 total；没有就是 null，UI 显示「已取 N 项」。
 *  2. **未知工具不许猜中文名**：查不到映射就原样显示工具名（`get_foo`），分组落「其他」。
 *     猜成「主力」比显示 `get_foo` 更糟 —— 用户会以为这个维度真的取到了。
 *  3. **失败要留痕**：事件带 error / ok:false 时状态是 failed，不被后续的「看起来还行」洗白。
 *  4. **不知道就说不知道**：`ok`（✓ / 算进「已取」）只认**完成证据**；没有完成证据的条目，
 *     流结束后落 `unknown`（未确认），不是 ok。
 *
 * 事件契约（2026-10-28 核对后端源码，基线 a6eb3c98）：
 *  - `src/harness/pipeline.py:425` / `pipeline_market.py:355` 只在工具**开跑**时
 *    `yield ("stage", "正在取数 i/N：tool")`；没有「跑完」帧，也没有 per-tool 成功帧。
 *  - 流式端点把它包成 `{"message": payload}`（`src/web/api/chat.py:2260`），前端拿到的是字符串，
 *    不含 `ok` / `status` 字段。
 *  - 每个工具的真实成功/失败只体现在收尾的 `("runs", (results, errors))`，errors 形如
 *    `"{label}:{err}"`（`pipeline_market.py:311`），目前**未下发前端**。
 *
 * 因此旧实现里那两处「推断」是错的，已删除：
 *  - 「下一条开跑 ⇒ 上一条已完成」：没有任何完成证据，工具可能仍在跑或已静默失败。
 *  - 「流结束 ⇒ pending 全成功」：更糟 —— 整条管线失败回落 tool loop 时也照样会结束。
 * 真失败只能靠显式失败事件或 `foldFailureNotices`（后端 errors 形状）落到 failed；
 * 两者都没有的条目，诚实的答案是 unknown。
 */

/**
 * pending = 开跑了，结果还不知道；ok = 有完成证据；failed = 有失败证据；
 * unknown = 本轮结束仍未确认（流结束/中断）—— 既不等于成功，也不栽赃成失败。
 */
export type ReadinessStatus = 'pending' | 'ok' | 'failed' | 'unknown'

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
  /** 本轮结束仍未确认的条数（既没成功证据也没失败证据）。不许并进 done。 */
  unknown: number
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
  /** 显式完成证据（`ok:true` / `status:'ok'|'done'`）。字符串帧永远是 false —— 它没有这个字段。 */
  ok: boolean
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

const OK_STATUSES = new Set(['ok', 'done', 'success', 'succeeded', 'complete', 'completed'])

/**
 * 显式完成证据。**只有** `ok:true` 或 `status` 是完成态才算 —— 没有字段、字段是别的值一律不算。
 * 失败优先于完成（同一帧里两个信号冲突时，宁可说未取到，也不说拿到了）。
 */
function okOfObject(evt: StageEventObject): boolean {
  if (failedOfObject(evt)) return false
  if (evt.ok === true) return true
  return OK_STATUSES.has((evt.status || '').toLowerCase())
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
      // 字符串帧（后端 SSE 的实际形状）不带完成字段：认得出失败文案就 failed，否则只是「开跑了」
      ok: false,
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
    ok: okOfObject(event),
    index: parsed?.index ?? null,
    total: parsed?.total ?? null,
  }
}

// ── reducer ────────────────────────────────────────────────────────────────

export function emptyReadiness(): ReadinessState {
  return { items: [], done: 0, total: null, failed: 0, unknown: 0 }
}

function recount(items: ReadinessItem[], total: number | null): ReadinessState {
  let done = 0
  let failed = 0
  let unknown = 0
  for (const it of items) {
    if (it.status === 'ok') done += 1
    else if (it.status === 'failed') failed += 1
    else if (it.status === 'unknown') unknown += 1
  }
  return { items, done, total, failed, unknown }
}

/**
 * 折一条事件进去（纯函数，返回新 state）。
 *
 * 状态机（只用**显式证据**改状态，绝不靠时间推移）：
 *  - 新工具 → 追加一条：有失败证据 → failed，没有 → pending（开跑，结果未知）；
 *  - 已有工具 → 有失败证据 → failed；有完成证据 → ok；两者都没有 → 保持原状
 *    （failed 粘性，已确认的 ok 也不因为一条重试开跑帧就退回未知）；
 *  - **其它条目一条都不动**：下一条开跑不能证明上一条已返回（见文件头的事件契约）。
 */
export function foldStageEvent(state: ReadinessState, event: StageEvent): ReadinessState {
  const ev = normalizeStageEvent(event)
  if (!ev) return state

  const total = ev.total ?? state.total
  const items = state.items.map((it) => {
    if (it.key !== ev.tool) return it
    if (ev.failed) return { ...it, status: 'failed' as ReadinessStatus }
    if (ev.ok) return { ...it, status: 'ok' as ReadinessStatus }
    // 没有任何结果证据：开跑帧只证明「又跑了一次」，状态不可推进
    if (it.status === 'ok' || it.status === 'failed') return it
    return { ...it, status: 'pending' as ReadinessStatus }
  })

  if (!items.some((it) => it.key === ev.tool)) {
    items.push({
      key: ev.tool,
      label: ev.label,
      group: ev.group,
      status: ev.failed ? 'failed' : ev.ok ? 'ok' : 'pending',
    })
  }

  return recount(items, total)
}

export function foldStageEvents(events: StageEvent[]): ReadinessState {
  let state = emptyReadiness()
  for (const ev of events) state = foldStageEvent(state, ev)
  return state
}

/**
 * 流结束：还在 pending 的落 **unknown（未确认）**，不是 ok。
 *
 * 「回答写完了」只证明这一轮结束了，不证明某个维度的数据到手了 —— 整条管线失败回落 tool loop
 * 时同样会正常结束。已确认的 ok / 已留痕的 failed 原样保留。
 */
export function settleReadiness(state: ReadinessState): ReadinessState {
  if (!state.items.length) return state
  const items = state.items.map((it) =>
    it.status === 'pending' ? { ...it, status: 'unknown' as ReadinessStatus } : it,
  )
  return recount(items, state.total)
}

/**
 * 用运行轨迹（`GET /copilot/runs/{run_id}` 的 steps）把 `unknown` 补成 ok / failed。
 *
 * 背景（2026-10-31 线上）：SSE 的 `stage` 只在工具**开跑**时发一条，没有完成帧，
 * 于是每条回答都挂着「已取 0/13 · 未确认 13」——正文却写满了数据，用户读成"13 项都没取到、
 * 这篇是编的"。而服务端其实**逐工具记录了 ok/error**（`CopilotStep.ok`），
 * 只是没走这条路回到界面。
 *
 * 规则（与 foldFailureNotices 同一条克制原则）：
 *  - 只认**工具名精确命中**的 step；一个工具多条 step（重试/多标的）以「有任一成功即 ok」为准，
 *    全部失败才 failed —— 多标的扫描里单只失败不等于整维度没数据；
 *  - steps 里没有的条目保持原状（unknown 不会被猜成 ok）；
 *  - 已有 ok/failed 的条目不被覆盖（流里已知的事实优先，轨迹只补缺口）。
 */
export function applyRunSteps(
  state: ReadinessState,
  steps: Array<{ tool?: string | null; ok?: boolean | null }> | null | undefined,
): ReadinessState {
  if (!state.items.length || !steps || !steps.length) return state
  const byTool = new Map<string, boolean[]>()
  for (const st of steps) {
    const tool = (st?.tool || '').trim()
    if (!tool) continue
    const list = byTool.get(tool) ?? []
    list.push(st.ok !== false)  // ok 缺失按成功算？不：只把显式 false 当失败，其余视为未断言
    byTool.set(tool, list)
  }
  if (!byTool.size) return state
  let changed = false
  const items = state.items.map((it) => {
    if (it.status === 'ok' || it.status === 'failed') return it
    const flags = byTool.get(it.key)
    if (!flags || !flags.length) return it
    changed = true
    const allFailed = flags.every((f) => f === false)
    return { ...it, status: (allFailed ? 'failed' : 'ok') as ReadinessStatus }
  })
  return changed ? recount(items, state.total) : state
}

/** 便捷入口：一串 stage 文案 → 终态（流结束后用）。 */
export function readinessFromLabels(labels: string[]): ReadinessState {
  return settleReadiness(foldStageEvents(labels))
}

/**
 * 折入后端 `errors` 列表（`"{label}:{err}"`，见 `pipeline_market.py:311`）—— 真实失败证据。
 *
 * 归属规则：label 能唯一命中一个条目（按中文 label 或工具名）才算 failed；
 * 认不出、或同时命中多条（歧义）就**不动**。宁可显示「未确认」，也不把失败栽给错的维度，
 * 更不会因为「有一条错误通知」就把别的条目变成功。
 */
export function foldFailureNotices(
  state: ReadinessState,
  notices: Array<string | null | undefined>,
): ReadinessState {
  if (!state.items.length || !notices.length) return state

  const hits = new Set<string>()
  const ambiguous = new Set<string>()
  for (const raw of notices) {
    const notice = (raw || '').trim()
    if (!notice) continue
    const m = /^([^:：]{1,40})[:：]\s*(.+)$/.exec(notice)
    if (!m) continue // 不是 `label:err` 形状 → 不猜
    const label = m[1].trim()
    if (!label) continue
    const matched = state.items.filter((it) => it.label === label || it.key === label)
    if (matched.length === 1) hits.add(matched[0].key)
    else if (matched.length > 1) matched.forEach((it) => ambiguous.add(it.key))
  }
  if (!hits.size && !ambiguous.size) return state

  let changed = false
  const items = state.items.map((it) => {
    if (ambiguous.has(it.key) || !hits.has(it.key)) return it
    if (it.status === 'failed') return it
    changed = true
    return { ...it, status: 'failed' as ReadinessStatus }
  })
  return changed ? recount(items, state.total) : state
}

/**
 * 计数文案。分母未知时只说「已取 N 项」，**不写 N/-- 也不写 N/1**。
 */
export function readinessProgressLabel(state: Pick<ReadinessState, 'done' | 'total'>): string {
  if (state.total == null) return `已取 ${state.done} 项`
  return `已取 ${state.done}/${state.total}`
}

/** 单条状态的可见文案（✓ / ⏳ / 未取到 / 未确认）。failed 读「未取到」而不是「失败」—— 说人话，且不夸大。 */
export function statusText(status: ReadinessStatus): string {
  if (status === 'ok') return '✓'
  if (status === 'failed') return '未取到'
  if (status === 'unknown') return '未确认'
  return '⏳'
}
