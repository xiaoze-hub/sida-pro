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
}

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/
const DIGEST_RE = /^[0-9a-f]{64}$/

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function asDay(v: unknown): string | null {
  if (typeof v !== 'string') return null
  const s = v.slice(0, 10)
  return DAY_RE.test(s) ? s : null
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
    fractals.push({
      kind,
      status,
      positionDay: day,
      positionPrice: price,
      confirmedOn: asDay(f.confirmed_on),
    })
  }
  // 升序由服务端保证；这里再按发生日稳定排序，避免上游顺序漂移导致 marker 断言失败
  fractals.sort((a, b) => (a.positionDay < b.positionDay ? -1 : a.positionDay > b.positionDay ? 1 : 0))

  return {
    symbol: asText(rec.symbol),
    instrument: asText(rec.instrument),
    snapshotDigest: rec.snapshot_digest,
    rulesVersion: asText(rec.rules_version),
    asOfDay: asDay(rec.as_of_day),
    omittedLatestReason: asText(rec.omitted_latest_reason),
    // 缺失时按后端同口径的默认值呈现（unknown/unverified），不写成"已复权/已终局"
    adjustment: asText(rec.adjustment) ?? 'unknown',
    finality: asText(rec.finality) ?? 'unverified',
    fractals,
    blocked,
    dropped,
  }
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

// ── 图层状态机（展示文案也走纯函数，禁"沉默失败"） ─────────────────────

export type ChanLayerView =
  | { phase: 'off' }
  | { phase: 'identity-unknown' }
  | { phase: 'unsupported'; detail: string }
  | { phase: 'not-daily' }
  | { phase: 'awaiting-bars' }
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
      return '缠论分型需要显式标的类型（stock/index）；当前页面未提供，已禁用（000001 这类同码不得猜）'
    case 'unsupported':
      return view.detail
    case 'not-daily':
      return '缠论分型只按日 K 口径提供（服务端仅 1d），当前周期不请求、不绘制'
    case 'awaiting-bars':
      return '等待当日 K 线就绪后再绑定缠论结构…'
    case 'bars-unreliable':
      return view.audit.detail
    case 'no-snapshot':
      return view.detail
    case 'loading':
      return '缠论分型加载中…'
    case 'ready': {
      const c = view.structure.fractals.filter((f) => f.status === 'confirmed').length
      const p = view.structure.fractals.filter((f) => f.status === 'candidate').length
      const bits = [`已确认 ${c} 个`, `待确认 ${p} 个`]
      if (view.structure.blocked > 0) bits.push(`blocked ${view.structure.blocked} 个（不绘为结构）`)
      if (view.structure.dropped > 0) bits.push(`脏行 ${view.structure.dropped} 个已丢弃`)
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
