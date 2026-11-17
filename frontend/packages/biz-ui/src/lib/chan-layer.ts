/**
 * 缠论分型图层（2026-11-17）—— **纯逻辑**：服务端结构清洗 + 记号映射 + 快照绑定/数据完整性裁决。
 *
 * 数据源（两条请求，缺一不可）：
 *  1. 图表主数据 `GET /klines/{symbol}?market=CN&days=..&interval=1d&instrument=stock|index`
 *     的响应里带 `chan_snapshot.digest`（该**图**内容的内容摘要）；
 *  2. `GET /klines/{symbol}/chan-structure?...&snapshot=<digest>` 只回**这一份图**算出的分型。
 *
 * 铁律（与后端 `src/web/api/chan_snapshot.py` 的契约一一对应）：
 *  - 前端**不自算**结构：只画服务端给的 `confirmed` / `candidate`；`blocked` 只计数，
 *    **永不**画成结构（那是"不足以确认"的包络，画出来就是把噪声当成结构）。
 *  - `blocked` / `confirmed` / `candidate` 的选择只认服务端 `status`，不按价格高低重判。
 *  - 图上记号**无买卖语义**：只标"顶/底"与"已确认/待确认"，不是买卖点（响应也明写不含买卖点）。
 *  - `blocked` 之外的诚实边界（复权口径 / 来源终局性 / 当日 K 是否参与）一律原样透出给用户，
 *    前端不猜、不补齐。
 *  - 结构与图的绑定靠 `snapshot_digest`：拿不到摘要 = 无法校验 = 不画（不"大概就是这张图"）。
 *
 * 这一层刻意做成无 React / 无 DOM 依赖的纯函数，便于单测把口径钉死。
 */

export type ChanFractalKind = 'top' | 'bottom'
export type ChanFractalStatus = 'confirmed' | 'candidate' | 'blocked'

/** 服务端 `fractals[]` 的一份（已清洗成驼峰命名）。 */
export interface ChanFractal {
  kind: ChanFractalKind
  status: ChanFractalStatus
  /** 分型发生日（服务端 `position_day`，YYYY-MM-DD） */
  positionDay: string
  /** 分型极值价（服务端 `position_price`） */
  positionPrice: number
  /** 确认日（`confirmed_on`）；candidate/blocked 时为 null */
  confirmedOn: string | null
}

/**
 * 笔（pen）图层（2026-11-17）—— 同一份已验快照的相邻结构连接。
 *
 * 口径铁律（与后端 `src/web/api/chan_snapshot.py` 的 `pens[]` 一一对应）：
 *  - 笔是**规则几何**：相邻两个已确认分型的合法连接，只画服务端给的端点；
 *    前端不插值、不"补中间日"、不自己找最近柱子（那就是给另一批数据配坐标）。
 *  - `blocked` 笔只计数，**永不**画成线；`confirmed` 细实线、`candidate` 细虚线。
 *  - 已确认笔必须带有效确认日（晚于起点、不越过 `as_of_day`）；候选笔必须没有确认日。
 *  - 笔链必须首尾相接（上一笔终点 == 下一笔起点）；对不上 = 疑似混了不同批次的数据，
 *    **整批拒收**（不做"能连就连"的部分绘制 —— 那会把两条不同来源的笔拼成一条假走势）。
 *  - 图上记号**无买卖语义**：线的方向（向上/向下笔）只表示结构走向，不是买卖信号。
 */
export type ChanPenDirection = 'up' | 'down'
export type ChanPenStatus = 'confirmed' | 'candidate' | 'blocked'

/** 一条笔被拒收的原因码（只用于计数/展示，不参与任何"猜一个"的兜底）。 */
export type ChanPenRejectReason =
  | 'pen-not-object'
  | 'pen-bad-direction'
  | 'pen-bad-status'
  | 'pen-bad-endpoint'
  | 'pen-bad-order'
  | 'pen-direction-mismatch'
  | 'pen-after-as-of'
  | 'pen-confirm-day-invalid'
  | 'pen-candidate-has-confirm'
  | 'pen-blocked-has-confirm'
  | 'pen-chain-broken'

/** 笔的一个端点（服务端 `start` / `end`；只留可绘制、可校验的三件事）。 */
export interface ChanPenEndpoint {
  kind: ChanFractalKind
  /** 端点发生日（`position_day`，YYYY-MM-DD） */
  positionDay: string
  /** 端点价格（`price`）：分型极值价，必须是有限正数 */
  price: number
}

/** 归一化后的一条可画笔（confirmed / candidate）。 */
export interface ChanPen {
  direction: ChanPenDirection
  status: 'confirmed' | 'candidate'
  /** 已确认笔的封口日；候选笔恒为 null */
  confirmedOn: string | null
  start: ChanPenEndpoint
  end: ChanPenEndpoint
}

/** 归一化后的笔批次（含"为什么有笔没画"的账）。 */
export interface ChanPenBatch {
  /** 可画的笔（服务端顺序 = 笔链顺序） */
  pens: ChanPen[]
  /** 服务端标记 blocked 的笔数 —— 只计数，不绘制 */
  penBlocked: number
  /** 被拒收的笔数（= `penRejects.length`） */
  penDropped: number
  penRejects: Array<{ index: number; reason: ChanPenRejectReason }>
  /** 整批不可用原因码（非 null 时 `pens` 必为空：不做部分绘制） */
  rejectReason: ChanPenRejectReason | null
  /** 本响应是否带 `pens` 字段（缺 = 后端未提供，图例如实说"笔未提供"） */
  provided: boolean
}

/** 归一化后的结构（只保留可校验、可绘制需要的字段）。 */
export interface ChanStructure {
  symbol: string | null
  instrument: string | null
  /** 与请求 `snapshot=` 应完全一致的摘要（64 位小写十六进制） */
  snapshotDigest: string
  rulesVersion: string | null
  asOfDay: string | null
  /** 当日 bar 是否被排除 + 原因（服务端 `omitted_latest_reason`，如 current_day_completion_unverified） */
  omittedLatestReason: string | null
  /** 复权口径（服务端恒为 unknown：适配器无法证明） */
  adjustment: string
  /** 来源终局性（服务端恒为 unverified） */
  finality: string
  /** 可画的分型（confirmed + candidate），按发生日升序；blocked **不在**这里 */
  fractals: ChanFractal[]
  /** 被服务端标记 blocked 的个数 —— 只计数，不绘制 */
  blocked: number
  /** 脏行被丢掉的个数（不静默改数据：丢掉就要说出来） */
  dropped: number
  /** 可画的笔（confirmed + candidate），服务端顺序（笔链顺序） */
  pens: ChanPen[]
  /** blocked 笔个数（只计数，不绘制） */
  penBlocked: number
  /** 被拒收的笔个数 */
  penDropped: number
  penRejects: Array<{ index: number; reason: ChanPenRejectReason }>
  /** 整批笔不可用原因码（如 `pen-chain-broken`）：非 null 时 `pens` 为空 */
  penRejectReason: ChanPenRejectReason | null
  /** 本响应是否带 `pens`（false = 后端未提供，不是"没有笔"） */
  pensProvided: boolean
}

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/
const DIGEST_RE = /^[0-9a-f]{64}$/

/**
 * 严格日历日校验（2026-11-17 复审修复）。
 *
 * 只过正则是不够的: `2026-02-30` / `2026-13-01` 都是**正则合法、日历非法**的值。
 * V8 的 `new Date('2026-02-30T00:00:00Z')` 不会给 Invalid Date, 而是**静默归一**到
 * `2026-03-02` —— 正好是图上真有一根 K 线的日子。于是"2 月 30 日的分型"会被画到
 * 3 月 2 日的柱子上, 而且看起来完全合法(这是最坏的错法: 错得不像错)。
 *
 * 唯一可靠判据是**回读比**: 用 UTC 构造再取回年月日, 三段都必须与输入逐字相同。
 * 不做任何"修正"(把 30 改成 28/29) —— 修正等于替服务端编数据。
 */
export function isValidCalendarDay(v: unknown): v is string {
  if (typeof v !== 'string' || !DAY_RE.test(v)) return false
  const y = Number(v.slice(0, 4))
  const m = Number(v.slice(5, 7))
  const d = Number(v.slice(8, 10))
  if (!Number.isInteger(y) || !Number.isInteger(m) || !Number.isInteger(d)) return false
  if (m < 1 || m > 12 || d < 1 || d > 31) return false
  const t = new Date(Date.UTC(y, m - 1, d))
  if (Number.isNaN(t.getTime())) return false
  return (
    t.getUTCFullYear() === y && t.getUTCMonth() === m - 1 && t.getUTCDate() === d
  )
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function asDay(v: unknown): string | null {
  if (typeof v !== 'string') return null
  const s = v.slice(0, 10)
  return isValidCalendarDay(s) ? s : null
}

function asText(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v.trim() : null
}

/**
 * 摘要是否为**完整** 64 位小写十六进制。
 * 不做 `toLowerCase()` / 不补位 —— 服务端明确"截短/大写一律拒绝"，
 * 前端若在这里"修好"，就会把请求绑定错了标的的摘要当成同一张图。
 */
export function isChanDigest(v: unknown): v is string {
  return typeof v === 'string' && DIGEST_RE.test(v)
}

// ── 笔（pen）归一化 ───────────────────────────────────────────────────
//
// 与分型同一套"服务端说什么就画什么"的口径，但笔多一条**批次**约束：笔是链，
// 链断了就不是同一批数据算出来的笔。对不上就整批拒收（不给"能连就连"的兜底）。

interface PenEndpointRead {
  kind: ChanFractalKind
  day: string
  price: number
}

function readPenEndpoint(v: unknown): PenEndpointRead | null {
  const rec = asRecord(v)
  if (!rec) return null
  const kind = rec.kind === 'top' ? 'top' : rec.kind === 'bottom' ? 'bottom' : null
  const day = asDay(rec.position_day)
  const raw = rec.price
  const price = typeof raw === 'number' ? raw : NaN
  if (!kind || !day) return null
  if (!Number.isFinite(price) || price <= 0) return null
  return { kind, day, price }
}

/** 端点是否逐字相同（日 + 价）—— 笔链连续的唯一判据，不做"差不多"的容差。 */
function sameEndpoint(a: PenEndpointRead, b: PenEndpointRead): boolean {
  return a.day === b.day && a.price === b.price
}

/**
 * `pens[]` → 归一化笔批次。
 *
 * 分三步（顺序有意义）：
 *  1. **单笔硬前提**：方向/状态可识别、两端点日期日历合法且价格有限正数、起点严格早于终点、
 *     方向与端点类型一致（up=底→顶）、端点不越过 `as_of_day`；不满足 → 拒收并记原因。
 *  2. **笔链连续性**（对所有端点合格的单笔，含 blocked）：上一笔终点必须逐字等于下一笔起点；
 *     一旦对不上，整批拒收（`pen-chain-broken`）—— 混批的笔不能拼成一条线。
 *     注意 blocked 也要参与链校验：服务端的链本来就把它们串在一起，跳过它们会造出假断裂。
 *  3. **状态语义**：blocked 只计数；confirmed 必须有有效确认日（> 起点日、≤ as_of_day）；
 *     candidate 必须没有确认日。任何一条不满足都拒收这条（不猜、不降级）。
 */
export function normalizeChanPens(raw: unknown, asOfDay: string | null): ChanPenBatch {
  const empty: ChanPenBatch = {
    pens: [],
    penBlocked: 0,
    penDropped: 0,
    penRejects: [],
    rejectReason: null,
    provided: false,
  }
  if (!Array.isArray(raw)) return empty

  const rejects: Array<{ index: number; reason: ChanPenRejectReason }> = []
  /** 端点/方向/截止日都合格的单笔（含 blocked）；链校验在下一轮做 */
  const staged: Array<{ index: number; direction: ChanPenDirection; status: ChanPenStatus; confirmedOn: string | null; start: PenEndpointRead; end: PenEndpointRead }> = []

  raw.forEach((item, index) => {
    const rec = asRecord(item)
    if (!rec) {
      rejects.push({ index, reason: 'pen-not-object' })
      return
    }
    const direction: ChanPenDirection | null =
      rec.direction === 'up' ? 'up' : rec.direction === 'down' ? 'down' : null
    if (!direction) {
      rejects.push({ index, reason: 'pen-bad-direction' })
      return
    }
    const status: ChanPenStatus | null =
      rec.status === 'confirmed' ? 'confirmed' : rec.status === 'candidate' ? 'candidate' : rec.status === 'blocked' ? 'blocked' : null
    if (!status) {
      rejects.push({ index, reason: 'pen-bad-status' })
      return
    }
    const start = readPenEndpoint(rec.start)
    const end = readPenEndpoint(rec.end)
    if (!start || !end) {
      rejects.push({ index, reason: 'pen-bad-endpoint' })
      return
    }
    if (start.day >= end.day) {
      rejects.push({ index, reason: 'pen-bad-order' })
      return
    }
    // 向上笔 = 底→顶，向下笔 = 顶→底；反了就不是这个方向的笔（不按价格重判方向）
    const directionOk =
      direction === 'up' ? start.kind === 'bottom' && end.kind === 'top' : start.kind === 'top' && end.kind === 'bottom'
    if (!directionOk) {
      rejects.push({ index, reason: 'pen-direction-mismatch' })
      return
    }
    if (asOfDay && end.day > asOfDay) {
      // 不许描述快照之后的走势（服务端 as_of_day 是硬边界）
      rejects.push({ index, reason: 'pen-after-as-of' })
      return
    }
    staged.push({
      index,
      direction,
      status,
      confirmedOn: asDay(rec.confirmed_on),
      start,
      end,
    })
  })

  // ② 笔链连续性：服务端顺序就是链顺序，不对就整批作废。
  let chainBroken = false
  for (let i = 1; i < staged.length; i += 1) {
    if (!sameEndpoint(staged[i - 1].end, staged[i].start)) {
      chainBroken = true
      for (let j = i; j < staged.length; j += 1) rejects.push({ index: staged[j].index, reason: 'pen-chain-broken' })
      break
    }
  }
  if (chainBroken) {
    // 整批作废: 被链断裂牵连的单笔也要记一笔账(否则"服务了 N 笔、画了 0 笔"这段差额
    // 在界面上无解释)。同一 index 只记一次, `penDropped` 就等于"被拒收的笔数"。
    const rejected = new Set(rejects.map((r) => r.index))
    for (const pen of staged) {
      if (!rejected.has(pen.index)) rejects.push({ index: pen.index, reason: 'pen-chain-broken' })
    }
    return {
      pens: [],
      penBlocked: 0,
      penDropped: rejects.length,
      penRejects: rejects.sort((a, b) => a.index - b.index),
      rejectReason: 'pen-chain-broken',
      provided: true,
    }
  }

  // ③ 状态语义 + 确认日
  let penBlocked = 0
  const pens: ChanPen[] = []
  for (const pen of staged) {
    if (pen.status === 'blocked') {
      // blocked 是"不足以确认"的包络: 带确认日就自相矛盾(服务端同样禁止),
      // 拒收而不是"当它是已确认"或"当它没确认日"。
      if (pen.confirmedOn !== null) {
        rejects.push({ index: pen.index, reason: 'pen-blocked-has-confirm' })
        continue
      }
      penBlocked += 1
      continue
    }
    if (pen.status === 'confirmed') {
      // 确认日必须自己站得住：合法日历日、晚于起点、不越过 as_of_day
      if (
        !pen.confirmedOn ||
        !isValidCalendarDay(pen.confirmedOn) ||
        pen.confirmedOn <= pen.start.day ||
        (asOfDay !== null && pen.confirmedOn > asOfDay)
      ) {
        rejects.push({ index: pen.index, reason: 'pen-confirm-day-invalid' })
        continue
      }
    } else if (pen.confirmedOn !== null) {
      // 候选笔带确认日 = 自相矛盾（不猜"是不是已经确认了"）
      rejects.push({ index: pen.index, reason: 'pen-candidate-has-confirm' })
      continue
    }
    pens.push({
      direction: pen.direction,
      status: pen.status,
      confirmedOn: pen.confirmedOn,
      start: { kind: pen.start.kind, positionDay: pen.start.day, price: pen.start.price },
      end: { kind: pen.end.kind, positionDay: pen.end.day, price: pen.end.price },
    })
  }

  return {
    pens,
    penBlocked,
    penDropped: rejects.length,
    penRejects: rejects.sort((a, b) => a.index - b.index),
    rejectReason: null,
    provided: true,
  }
}

/**
 * 结构响应 → 归一化结构；**任何一条硬前提不满足就返回 null**（调用方据此不画）。
 *
 * 硬前提：
 *  - 顶层是对象，且 `snapshot_digest` 是完整摘要（没有摘要 = 这份结构绑不到这张图）；
 *  - `fractals` 是数组（缺失 ≠ "没有分型"：后者会返回 `[]`）。
 *
 * 单行脏值（kind/status 不认识、日期不是 YYYY-MM-DD、价格非有限正数）→ 丢弃并计入 `dropped`。
 * 不猜 kind、不按价格补 status、不把 `blocked` 当 `candidate` 兜底。
 */
export function normalizeChanStructure(raw: unknown): ChanStructure | null {
  const rec = asRecord(raw)
  if (!rec) return null
  if (!isChanDigest(rec.snapshot_digest)) return null
  if (!Array.isArray(rec.fractals)) return null

  let blocked = 0
  let dropped = 0
  const fractals: ChanFractal[] = []
  for (const item of rec.fractals) {
    const f = asRecord(item)
    if (!f) {
      dropped += 1
      continue
    }
    const kind = f.kind === 'top' ? 'top' : f.kind === 'bottom' ? 'bottom' : null
    const status =
      f.status === 'confirmed' ? 'confirmed' : f.status === 'candidate' ? 'candidate' : f.status === 'blocked' ? 'blocked' : null
    const day = asDay(f.position_day)
    const price = typeof f.position_price === 'number' ? f.position_price : NaN
    const priceOk = Number.isFinite(price) && price > 0
    if (!kind || !status || !day || !priceOk) {
      dropped += 1
      continue
    }
    if (status === 'blocked') {
      // 只计数：blocked 不是有效结构，不进可画集合（也不在图上留任何痕迹）
      blocked += 1
      continue
    }
    // 「已确认」必须带**有效的确认日**且不早于分型日：确认日缺失/非法/倒挂 = 这条"确认"
    // 无法自证, 丢掉并计数（不替服务端补一个日期, 也不把 candidate 当 confirmed 兜底）。
    const confirmedOn = asDay(f.confirmed_on)
    if (status === 'confirmed' && (!confirmedOn || confirmedOn < day)) {
      dropped += 1
      continue
    }
    fractals.push({
      kind,
      status,
      positionDay: day,
      positionPrice: price,
      confirmedOn,
    })
  }
  // 升序由服务端保证；这里再按发生日稳定排序，避免上游顺序漂移导致 marker 断言失败
  fractals.sort((a, b) => (a.positionDay < b.positionDay ? -1 : a.positionDay > b.positionDay ? 1 : 0))

  // 笔：同一份响应里的 `pens[]`（后端一次取数、一次构建，分型与笔同源）。
  // 缺失（旧后端/未实现）记 `provided=false`，与"有 pens 但为空"区分开。
  const asOfDay = asDay(rec.as_of_day)
  const penBatch = normalizeChanPens(rec.pens, asOfDay)

  return {
    symbol: asText(rec.symbol),
    instrument: asText(rec.instrument),
    snapshotDigest: rec.snapshot_digest,
    rulesVersion: asText(rec.rules_version),
    asOfDay,
    omittedLatestReason: asText(rec.omitted_latest_reason),
    // 缺失时按后端同口径的默认值呈现（unknown/unverified），不写成"已复权/已终局"
    adjustment: asText(rec.adjustment) ?? 'unknown',
    finality: asText(rec.finality) ?? 'unverified',
    fractals,
    blocked,
    dropped,
    pens: penBatch.pens,
    penBlocked: penBatch.penBlocked,
    penDropped: penBatch.penDropped,
    penRejects: penBatch.penRejects,
    penRejectReason: penBatch.rejectReason,
    pensProvided: penBatch.provided,
  }
}

// ── 请求控制（可取消 + 真超时；2026-11-17 复审修复） ─────────────────────

/**
 * 结构请求的超时时限。服务端算一份结构是纯本地计算(读快照+分型), 正常远低于 1s;
 * 20s 已经是"后端排队/连接池被打满"的量级。超时必须**显式失败**, 不许永远 loading。
 */
export const CHAN_STRUCTURE_TIMEOUT_MS = 20000

export type ChanRequestResult =
  | { state: 'ok'; payload: unknown }
  | { state: 'timeout'; timeoutMs: number }
  | { state: 'aborted' }
  | { state: 'error'; error: unknown }

export interface ChanRequest {
  /** 一定 settle 一次(成功/失败/超时/取消) —— 调用方据此离开 loading */
  result: Promise<ChanRequestResult>
  /** 主动取消(切股/关图层/unmount): 立刻 abort 上游并 settle 为 aborted */
  cancel: () => void
  /** 是否因为超时而结束(用于文案) */
  timedOut: () => boolean
}

/**
 * 带**真超时**的可取消请求。
 *
 * 为什么不能只靠 `fetchAPI(..., { signal, timeoutMs })`: 该实现的超时计时器只在
 * **没有调用方 signal** 时才会建（`_attemptFetch`: `callerSignal ? null : new AbortController()`）
 * —— 一旦传了 signal, `timeoutMs` 被静默忽略。而结构请求必须传 signal(切股要能取消),
 * 于是"后端挂了"这条路径永远不 settle, 图层卡在「加载中…」。
 *
 * 这里把两件事拆开做:
 *  - **取消**: 自己的 `AbortController`, cancel 时 abort 上游;
 *  - **超时**: 自己的计时器, 到点先 abort 上游、再**独立地** settle 成 timeout ——
 *    即使上游连 abort 都不理(响应头到了、响应体悬挂, `res.json()` 永不返回),
 *    调用方也能拿到结果并离开 loading。
 *
 * 计时器在 settle 时一律清掉(不泄漏); 结果只写一次(事件竞态下不会"超时后再成功")。
 */
export function startChanRequest(opts: {
  timeoutMs: number
  /** 发起上游请求(拿到的 signal 必须透传给 fetch, 取消/超时才能真的断开) */
  request: (signal: AbortSignal) => Promise<unknown>
  /** 外部取消信号(可选) */
  signal?: AbortSignal
}): ChanRequest {
  const ctrl = new AbortController()
  const timeoutMs =
    Number.isFinite(opts.timeoutMs) && opts.timeoutMs > 0 ? opts.timeoutMs : CHAN_STRUCTURE_TIMEOUT_MS
  let settled = false
  let timerFired = false
  let timer: ReturnType<typeof setTimeout> | null = null
  let resolveResult: (r: ChanRequestResult) => void = () => {}
  const result = new Promise<ChanRequestResult>((resolve) => {
    resolveResult = resolve
  })

  const onExternalAbort = () => cancel()
  const settle = (r: ChanRequestResult) => {
    if (settled) return
    settled = true
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
    opts.signal?.removeEventListener?.('abort', onExternalAbort)
    resolveResult(r)
  }
  const cancel = () => {
    if (settled) return
    try {
      ctrl.abort()
    } catch {
      /* 已 abort / 环境不支持: 不影响 settle */
    }
    settle({ state: 'aborted' })
  }

  if (opts.signal?.aborted) {
    cancel()
    return { result, cancel, timedOut: () => false }
  }
  opts.signal?.addEventListener?.('abort', onExternalAbort, { once: true })

  timer = setTimeout(() => {
    timer = null
    timerFired = true
    try {
      ctrl.abort()
    } catch {
      /* ignore */
    }
    settle({ state: 'timeout', timeoutMs })
  }, timeoutMs)

  let pending: Promise<unknown>
  try {
    pending = opts.request(ctrl.signal)
  } catch (error) {
    settle({ state: 'error', error })
    return { result, cancel, timedOut: () => timerFired }
  }
  // 双分支 then: 上游拒绝(含 abort)不会变成未处理的 unhandled rejection
  void pending.then(
    (payload) => {
      if (settled) return
      if (timerFired) {
        settle({ state: 'timeout', timeoutMs })
        return
      }
      settle({ state: 'ok', payload })
    },
    (error) => {
      if (settled) return
      if (timerFired) {
        settle({ state: 'timeout', timeoutMs })
        return
      }
      if (ctrl.signal.aborted) {
        settle({ state: 'aborted' })
        return
      }
      settle({ state: 'error', error })
    },
  )

  return { result, cancel, timedOut: () => timerFired }
}

// ── 图表响应里的快照摘要（结构与图的绑定） ─────────────────────────────

export type ChanSnapshotProblem = 'missing' | 'identity-mismatch' | 'no-digest'

export interface ChanSnapshotPick {
  ok: boolean
  problem: ChanSnapshotProblem | null
  digest: string | null
  unavailableReason: string | null
  /** 人话说明（直接展示给用户/进 title） */
  detail: string
}

/**
 * 从**图表主数据响应**取 `chan_snapshot.digest`，并核对它自述的身份。
 *
 * 为什么必须核对 `instrument` / `interval`：A 股指数与个股**同码**（000001 = 上证指数 /
 * 平安银行）。图表按 `instrument=index` 取的是指数，摘要自述 `instrument=stock` 就说明
 * 这份响应不是那张图（跨 worker / 缓存串了），拿它去请求结构就是"给另一个标的画结构"。
 */
export function pickChanSnapshot(
  payload: unknown,
  expected: { instrument: 'stock' | 'index'; interval: string },
): ChanSnapshotPick {
  const rec = asRecord(payload)
  const snap = rec ? asRecord(rec.chan_snapshot) : null
  if (!snap) {
    return {
      ok: false,
      problem: 'missing',
      digest: null,
      unavailableReason: null,
      detail: '图表响应未带 chan_snapshot 摘要（后端未提供），无法把结构绑定到这张图，未请求结构',
    }
  }
  const reason = asText(snap.unavailable_reason)
  const selfInstrument = asText(snap.instrument)
  const selfInterval = asText(snap.interval)
  if (selfInstrument && selfInstrument !== expected.instrument) {
    return {
      ok: false,
      problem: 'identity-mismatch',
      digest: null,
      unavailableReason: reason,
      detail: `快照自述标的类型(${selfInstrument})与当前图(${expected.instrument})不一致，拒绝使用（同码个股/指数不得互证）`,
    }
  }
  if (selfInterval && selfInterval !== expected.interval) {
    return {
      ok: false,
      problem: 'identity-mismatch',
      digest: null,
      unavailableReason: reason,
      detail: `快照自述周期(${selfInterval})与当前图(${expected.interval})不一致，拒绝使用`,
    }
  }
  if (!isChanDigest(snap.digest)) {
    return {
      ok: false,
      problem: 'no-digest',
      digest: null,
      unavailableReason: reason,
      detail: reason
        ? `图表响应未给出可校验的快照摘要（${reason}），无法绑定结构，未请求`
        : '图表响应未给出可校验的快照摘要，无法绑定结构，未请求',
    }
  }
  return { ok: true, problem: null, digest: snap.digest, unavailableReason: null, detail: '' }
}

// ── K 线数据完整性（前端不许"洗过数据还给结构配坐标"） ─────────────────

export type ChanBarsIssue = 'no-bars' | 'invalid-ohlc' | 'duplicate-days' | 'unsorted-days'

export interface ChanBarsAudit {
  ok: boolean
  issue: ChanBarsIssue | null
  /** OHLC 非法被前端剔除的行数 */
  dropped: number
  /** 重复交易日个数 */
  duplicates: number
  /** 原序列日期是否非升序 */
  outOfOrder: boolean
  /** 人话说明（状态条直接展示） */
  detail: string
}

/**
 * 审计「原始 K 线序列」是否会被前端渲染链**改写**（过滤 / 排序 / 去重）。
 *
 * 为什么这决定了结构能不能画：图上的蜡烛是前端处理**之后**的那份数据（`KlineChart`
 * 会剔除 OHLC 非法行、按日期升序、同日去重），而结构端点的摘要绑定的是**原始响应**。
 * 一旦这两份数据不再逐根相同，把结构画上去就是给"另一批数据"配坐标 —— 例如两行同日
 * K 线里被丢掉的那根正好是极值，分型位置就整体错开一格。这种时候唯一诚实的做法是
 * **不画并说明原因**，而不是"看起来差不多"。
 */
export function auditBarsTransform(raw: unknown): ChanBarsAudit {
  const rows = Array.isArray(raw) ? raw : []
  let dropped = 0
  let duplicates = 0
  let outOfOrder = false
  const seen = new Set<string>()
  let prevKey: string | null = null
  let maxKey: string | null = null

  for (const item of rows) {
    const rec = asRecord(item)
    const o = rec?.open
    const h = rec?.high
    const l = rec?.low
    const c = rec?.close
    const numeric = (v: unknown) => typeof v === 'number' && Number.isFinite(v)
    if (!rec || !numeric(o) || !numeric(h) || !numeric(l) || !numeric(c)) {
      dropped += 1
      continue
    }
    const day = asDay(rec?.date)
    if (!day) {
      // 日期不可解析 = 无法定位到某根 K 线；与"OHLC 非法"同样属于被剔除的行
      dropped += 1
      continue
    }
    if (seen.has(day)) duplicates += 1
    else seen.add(day)
    if (maxKey !== null && day < maxKey) outOfOrder = true
    if (prevKey !== null && day < prevKey) outOfOrder = true
    prevKey = day
    if (maxKey === null || day > maxKey) maxKey = day
  }

  if (rows.length === 0) {
    return {
      ok: false,
      issue: 'no-bars',
      dropped: 0,
      duplicates: 0,
      outOfOrder: false,
      detail: '图表没有 K 线数据，结构无从对齐，未请求',
    }
  }
  if (dropped > 0) {
    return {
      ok: false,
      issue: 'invalid-ohlc',
      dropped,
      duplicates,
      outOfOrder,
      detail: `原 K 线有 ${dropped} 行 OHLC/日期不合法，前端渲染时会剔除；结构与这张图已不是同一份数据，拒绝绘制`,
    }
  }
  if (duplicates > 0) {
    return {
      ok: false,
      issue: 'duplicate-days',
      dropped,
      duplicates,
      outOfOrder,
      detail: `原 K 线含 ${duplicates} 个重复交易日，前端渲染时会去重（先到先留）；结构对应的不是去重后那几根，拒绝绘制`,
    }
  }
  if (outOfOrder) {
    return {
      ok: false,
      issue: 'unsorted-days',
      dropped,
      duplicates,
      outOfOrder,
      detail: '原 K 线日期非升序，前端渲染时会重排；结构对应的是重排前的顺序，拒绝绘制',
    }
  }
  return { ok: true, issue: null, dropped, duplicates, outOfOrder, detail: '' }
}

// ── 记号映射（只画 confirmed / candidate，无买卖语义） ─────────────────

export interface ChanMarkerColors {
  /** 顶分型色（结构色，非涨跌/买卖语义） */
  top: string
  /** 底分型色 */
  bottom: string
}

export interface ChanMarkerSpec {
  day: string
  kind: ChanFractalKind
  status: 'confirmed' | 'candidate'
  price: number
  position: 'aboveBar' | 'belowBar'
  shape: 'circle'
  size: 1 | 2
  color: string
  /** 图上文字：只写"顶/底"（待确认加 `○`）；绝不写 买/卖/B/S */
  text: string
}

/** `#RRGGBB` → 半透明 rgba（待确认用同色压淡，不另造一套语义色）；非 6 位 hex 原样返回。 */
export function chanColorWithAlpha(hex: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec((hex || '').trim())
  if (!m) return hex
  const n = parseInt(m[1], 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

/**
 * 结构 → 主图记号。
 *
 * 视觉区分（无买卖语义）：
 *  - 顶分型标在柱**上方**、底分型标在柱**下方**（这就是"顶/底"的几何含义）；
 *  - 已确认：实心 + 大号（size 2）+ 纯色，文字 `顶`/`底`；
 *  - 待确认：同色压淡 + 小号（size 1）+ `○` 前缀，文字 `○顶`/`○底` ——
 *    与站内既有"空心=待确认"约定一致，不引入新图例语言。
 *  - `blocked` 根本不进这个函数（`normalizeChanStructure` 已剔除）。
 */
export function buildChanMarkers(structure: Pick<ChanStructure, 'fractals'>, colors: ChanMarkerColors): ChanMarkerSpec[] {
  const out: ChanMarkerSpec[] = []
  for (const f of structure.fractals) {
    if (f.status !== 'confirmed' && f.status !== 'candidate') continue
    // 日期必须过严格日历校验 + 确认日期必须有效且不早于分型日 —— 画之前再验一次,
    // 免得调用方绕开 `normalizeChanStructure` 直接喂脏数据时把记号落到别的柱子上。
    if (!isValidCalendarDay(f.positionDay)) continue
    if (f.status === 'confirmed' && (!isValidCalendarDay(f.confirmedOn) || f.confirmedOn < f.positionDay)) continue
    const base = f.kind === 'top' ? colors.top : colors.bottom
    const label = f.kind === 'top' ? '顶' : '底'
    const confirmed = f.status === 'confirmed'
    out.push({
      day: f.positionDay,
      kind: f.kind,
      status: f.status,
      price: f.positionPrice,
      position: f.kind === 'top' ? 'aboveBar' : 'belowBar',
      shape: 'circle',
      size: confirmed ? 2 : 1,
      color: confirmed ? base : chanColorWithAlpha(base, 0.55),
      text: confirmed ? label : `○${label}`,
    })
  }
  return out
}

// ── 笔 → 图上线段（只用服务端端点，不插值/不跨日造点） ─────────────────────

export interface ChanPenColors {
  /** 已确认笔：细实线 */
  confirmed: string
  /** 待确认笔：细虚线（同色压淡） */
  candidate: string
}

/**
 * 一条笔在图上要画的那条线。
 *
 * `status` 只表达"已确认/待确认"（线型），**不表达买卖**；`direction` 只描述结构走向
 * （向上笔=底→顶、向下笔=顶→底），也不是买卖方向。
 */
export interface ChanPenSegment {
  /** 可溯源的稳定键：`起点日>终点日`（笔链单调，故唯一） */
  key: string
  direction: ChanPenDirection
  status: 'confirmed' | 'candidate'
  startDay: string
  startPrice: number
  endDay: string
  endPrice: number
  /** true = 虚线（待确认）；false = 实线（已确认） */
  dashed: boolean
  lineWidth: 1
  color: string
}

export type ChanPenSkipReason = 'invalid-pen' | 'endpoint-day-not-on-chart'

export interface ChanPenSkip {
  key: string
  reason: ChanPenSkipReason
  startDay: string
  endDay: string
}

/**
 * 可画笔 → 图表线段（**每条只有两个点**：服务端起点/终点）。
 *
 * 两条定位铁律：
 *  - 两个端点日都必须是**当前画面上真有的一根 K 线**（`barDays`）。少一天就整笔不画，
 *    并计入 `skipped` —— 绝不把端点贴到"最近的柱子"上（那是给另一批数据配坐标）。
 *  - 只连服务端自己给出的这一对端点：不按笔链"补点"、不跨笔连线（跨笔连线就是把不同
 *    批次的笔拼成一条假走势）。
 */
export function buildChanPenSegments(
  structure: Pick<ChanStructure, 'pens'>,
  opts: { barDays: ReadonlySet<string>; colors: ChanPenColors },
): { segments: ChanPenSegment[]; skipped: ChanPenSkip[] } {
  const segments: ChanPenSegment[] = []
  const skipped: ChanPenSkip[] = []
  for (const pen of structure.pens) {
    const key = `${pen.start.positionDay}>${pen.end.positionDay}`
    // 画之前把硬前提再验一次：调用方绕开 normalize 直接喂脏数据时也不许落到别的柱子上
    const valid =
      isValidCalendarDay(pen.start.positionDay) &&
      isValidCalendarDay(pen.end.positionDay) &&
      pen.start.positionDay < pen.end.positionDay &&
      Number.isFinite(pen.start.price) &&
      pen.start.price > 0 &&
      Number.isFinite(pen.end.price) &&
      pen.end.price > 0 &&
      (pen.status === 'confirmed'
        ? isValidCalendarDay(pen.confirmedOn) && pen.confirmedOn > pen.start.positionDay
        : pen.confirmedOn === null)
    if (!valid) {
      skipped.push({ key, reason: 'invalid-pen', startDay: pen.start.positionDay, endDay: pen.end.positionDay })
      continue
    }
    if (!opts.barDays.has(pen.start.positionDay) || !opts.barDays.has(pen.end.positionDay)) {
      skipped.push({
        key,
        reason: 'endpoint-day-not-on-chart',
        startDay: pen.start.positionDay,
        endDay: pen.end.positionDay,
      })
      continue
    }
    const confirmed = pen.status === 'confirmed'
    segments.push({
      key,
      direction: pen.direction,
      status: pen.status,
      startDay: pen.start.positionDay,
      startPrice: pen.start.price,
      endDay: pen.end.positionDay,
      endPrice: pen.end.price,
      dashed: !confirmed,
      lineWidth: 1,
      color: confirmed ? opts.colors.confirmed : opts.colors.candidate,
    })
  }
  return { segments, skipped }
}

/**
 * 「当前画面可绑定的快照」信号（`KlineChart.onChanSnapshotChange` 的载荷）。
 *
 * 事件语义（消费方按此绑定自己的结构/笔查询）：
 *  - 非 null 只在**同一张已落图的日 K** + 摘要可校验（64 位小写 hex）+ 前端渲染链未改写
 *    原始 K 线（`audit.ok`）+ 身份/周期/天数与当前 props 一致时出现；
 *  - 身份字段全部是**已核实事实**：`symbol`/`market` 来自图表请求，`instrument` 来自响应的
 *    `is_index` 标记（缺失则不产生信号，绝不按代码猜），`days` 是本次请求的真实天数；
 *  - 切标的/周期/天数、K 线加载失败、重读开始、卸载、响应说"图已变(409)"→ `null`；
 *  - 结构/笔请求**取数失败**（超时/5xx）不改变信号：它描述的是"这张图绑定哪份快照"，
 *    不是"结构有没有画出来"；
 *  - 值没变不重复回调（幂等），不发消息、不弹提示。
 */
export interface ChanChartSnapshot {
  symbol: string
  market: string
  instrument: 'stock' | 'index'
  /** 恒为 '1d'：结构与笔只按日线口径提供 */
  interval: '1d'
  /** 与图表主数据请求一致的天数 */
  days: number
  /** 图表响应 `chan_snapshot.digest`：可直接回传给 `/chan-structure?snapshot=` */
  digest: string
}

// ── 图层状态机（展示文案也走纯函数，禁"沉默失败"） ─────────────────────

export type ChanLayerView =
  | { phase: 'off' }
  | { phase: 'identity-unknown' }
  | { phase: 'unsupported'; detail: string }
  | { phase: 'not-daily' }
  | { phase: 'awaiting-bars' }
  | { phase: 'bars-failed'; detail: string }
  | { phase: 'bars-unreliable'; audit: ChanBarsAudit }
  | { phase: 'no-snapshot'; detail: string }
  | { phase: 'loading' }
  | { phase: 'ready'; structure: ChanStructure }
  | { phase: 'snapshot-mismatch'; detail: string }
  | { phase: 'error'; status: number | null; detail: string }

/** 状态条文案：每个 phase 都必须有话说（缺数据/不一致/被拒绝/当日K排除/口径未知都不许沉默）。 */
export function chanStatusText(view: ChanLayerView): string {
  switch (view.phase) {
    case 'off':
      return ''
    case 'identity-unknown':
      return '缠论结构（分型/笔）需要显式标的类型（stock/index）；当前页面未提供，已禁用（000001 这类同码不得猜）'
    case 'unsupported':
      return view.detail
    case 'not-daily':
      return '缠论结构（分型/笔）只按日 K 口径提供（服务端仅 1d），当前周期不请求、不绘制'
    case 'awaiting-bars':
      return '等待当日 K 线就绪后再绑定缠论结构…'
    case 'bars-failed':
      return view.detail
    case 'bars-unreliable':
      return view.audit.detail
    case 'no-snapshot':
      return view.detail
    case 'loading':
      return '缠论结构（分型/笔）加载中…'
    case 'ready': {
      const c = view.structure.fractals.filter((f) => f.status === 'confirmed').length
      const p = view.structure.fractals.filter((f) => f.status === 'candidate').length
      const bits = [`分型 已确认 ${c} 个`, `待确认 ${p} 个`]
      if (view.structure.blocked > 0) bits.push(`blocked ${view.structure.blocked} 个（不绘为结构）`)
      if (view.structure.dropped > 0) bits.push(`脏行 ${view.structure.dropped} 个已丢弃`)
      if (view.structure.pensProvided) {
        const pc = view.structure.pens.filter((x) => x.status === 'confirmed').length
        const pp = view.structure.pens.filter((x) => x.status === 'candidate').length
        bits.push(`笔 已确认 ${pc} · 待确认 ${pp}`)
        if (view.structure.penBlocked > 0) bits.push(`blocked 笔 ${view.structure.penBlocked} 笔（不绘）`)
        if (view.structure.penDropped > 0) bits.push(`笔脏行 ${view.structure.penDropped} 笔已丢弃`)
        if (view.structure.penRejectReason) bits.push(`笔不可用（${view.structure.penRejectReason}）`)
      } else {
        bits.push('笔未提供（本响应不含 pens）')
      }
      if (view.structure.omittedLatestReason) {
        bits.push(`当日 K 未参与（${view.structure.omittedLatestReason}）`)
      }
      bits.push(`复权口径 ${view.structure.adjustment}`, `终局性 ${view.structure.finality}`)
      return `已绑定快照：${bits.join(' · ')}`
    }
    case 'snapshot-mismatch':
      return `${view.detail}；已拒绝绘制（结构与这张图不一致，不能当同一结论用）`
    case 'error':
      return view.detail
    default:
      return ''
  }
}
