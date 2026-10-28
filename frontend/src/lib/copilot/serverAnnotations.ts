/**
 * 服务端情景标注 → 图表价位线。**本文件是唯一允许把"数字"变成"可画线"的地方。**
 *
 * ## 为什么要有这个文件（2026-10-28 P2 三次收口）
 *
 * 上一轮止血把画线全关掉（`SERVER_ANNOTATION_LINK_READY = false`）。关掉是对的，但
 * 「用户确认服务端情景标注后价位要能落在 K 线上」这条产品线仍没通。这次不靠"本地凭证"，
 * 只靠**服务端响应**：
 *
 *     参谋提议(propose) → CopilotAction(pending)
 *        │  用户点确认
 *        ▼
 *     POST /copilot/actions/{id}/apply  ──▶ {id, kind:'annotation', status:'applied', payload:{symbol,price,label,…}}
 *        │                                     ↑ 服务端按 user_id 过滤，非本人 400「动作不存在」
 *        ▼
 *     GET  /copilot/annotations?symbol&market&period ──▶ KlineAnnotation 行（本人 + 全局）
 *        │                                     ↑ pending 提议不在本表 ⇒ 未确认永远读不到
 *        ▼
 *     parseAnnotationsResponse() → annotationLinesFromRows() → 图表价位线
 *
 * 价位、标的、标签**全部逐字取自服务端行**。客户端对象、sessionStorage、事件 detail
 * 都不参与取值 —— 它们最多只能决定"我请求过哪些标注 id"，取不到对应服务端行就一条不画。
 *
 * ## 已核验的后端契约（含缺口，2026-10-28 逐条读码确认）
 *
 * 核验来源：`src/web/api/copilot.py`（`@router.get("/annotations")` 与 `@router.post("/actions/{id}/apply")`）、
 * `src/harness/actions.py`（`apply_action` / `_apply_annotation` / `rollback_action`）、
 * `src/db/models.py`（`KlineAnnotation`）、`src/web/response.py`（统一 `{code,data}` 包装）。
 *
 * | 事实 | 结论 | 依据 |
 * | --- | --- | --- |
 * | 作用域 | 服务端按 `user_id == 我 OR user_id IS NULL` 过滤（全局行也下发） | copilot.py get_copilot_annotations |
 * | 响应字段 | `id/price/ts/label/note/color/source/created_at` | 同上 |
 * | **无 action 关联** | `KlineAnnotation` 表**没有** `action_id` 列，响应也不带 → 刷新后无法证明"这条线来自哪次确认" | models.py:2520 / `_apply_annotation` 只写 source="ai" |
 * | **无归属回显** | 响应不回 `user_id`/`scope` → 分不清"我确认的"与"全局(user_id IS NULL)" | 同上 |
 * | **不回显请求键** | `symbol/market/period` 只在 query，响应里没有 → 标的只能靠"请求—响应"配对 | 同上 |
 * | 未确认不会上图 | pending 提议存在 `CopilotAction`，本接口只查 `KlineAnnotation` | actions.py `_apply_annotation` 是唯一 AI 写入点 |
 * | **撤销不可用** | `rollback_action` 对 `kind="annotation"` 直接 400「annotation 动作不支持回滚」，也没有 DELETE 接口 | actions.py rollback_action |
 *
 * 因此本文件只承诺它能证明的东西：
 *  - **画出来的价位**：服务端行里的 price（强约束，见 {@link parseAnnotationsResponse}）；
 *  - **画出来的对象**：服务端行里的 annotation id（唯一可追溯标识）；
 *  - **action 溯源**：只在"确认那一拍"的 apply 响应里带得出来，刷新后如实说"不可用"，
 *    绝不从本地存储里编一个 action id 出来（见 {@link SERVER_ANNOTATION_CONTRACT.actionIdInRow}）。
 */

// ---------------------------------------------------------------------------
// 后端契约（可执行的事实表：UI 文案与测试都读它，别在别处再抄一遍）
// ---------------------------------------------------------------------------

export const SERVER_ANNOTATION_CONTRACT = {
  /** 读取接口（JWT 作用域内，本人 + 全局） */
  readPath: 'GET /copilot/annotations',
  /** 确认接口（服务端按 user_id 过滤，非本人 400） */
  applyPath: 'POST /copilot/actions/{id}/apply',
  /** 响应行是否带 CopilotAction id —— **false**：KlineAnnotation 表没有这一列 */
  actionIdInRow: false,
  /** 响应行是否带回属主/作用域（"我的" vs "全局"）—— **false** */
  ownerScopeInRow: false,
  /** 响应行是否回显 symbol/market/period —— **false**（只能靠请求—响应配对） */
  echoesRequestKeys: false,
  /** 是否有撤销/删除图注的服务端接口 —— **false**（rollback 对 annotation 返回 400） */
  undoSupported: false,
  /** 未确认的 pending 提议会不会出现在读取接口里 —— **false**（只查 KlineAnnotation） */
  pendingListed: false,
  /** 标的族：后端把 A 股市场代码按一组处理（CN/SH/SZ/BJ 互通） */
  aShareMarkets: ['CN', 'SH', 'SZ', 'BJ'] as const,
} as const

export type AnnotationMarket = 'CN' | 'HK' | 'US'

/** 一次读取请求的键（标的 / 市场 / 周期）—— 响应不带这些字段，配对靠它。 */
export interface AnnotationRequest {
  symbol: string
  market: string
  period: string
}

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

/** 服务端 `GET /copilot/annotations` 的一行（已严格校验后的形状）。 */
export interface ServerAnnotationRow {
  /** KlineAnnotation.id —— 唯一可追溯的服务端标识 */
  id: number
  /** 水平线价位；时间点标注为 null */
  price: number | null
  /** 时间点标注（ISO 或 2026-10-20）；水平线为 null */
  ts: string | null
  label: string
  note: string | null
  color: 'up' | 'down' | 'other'
  /** 服务端写入方：ai=服务端标注（当前唯一写入点是用户确认的 apply），user=人工画的 */
  source: 'ai' | 'user'
  createdAt: string
}

/** 把一行标注压成"能画到 K 线上的价位线"。 */
export interface ServerAnnotationLine {
  /** 服务端标注 id（图上/文案里都用它溯源） */
  annotationId: number
  /** 服务端 CopilotAction id —— 只在确认那一拍的 apply 响应里能证实；刷新后不补 */
  actionId?: number
  price: number
  kind: 'support' | 'pressure'
  label: string
  source: 'ai' | 'user'
  /** 请求键（服务端不回声字段，取自本次请求） */
  symbol: string
  period: string
}

/** apply 响应里被证实"已确认"的标注动作。 */
export interface AppliedAnnotationAction {
  actionId: number
  symbol: string
  market: string
  period: string
  label: string
  price: number
  /** payload.source —— 服务端情景矩阵写入 "scenario_matrix"；其它值如实保留 "unknown" */
  origin: string
}

export type ParseRejectReason =
  | 'shape'
  | 'field'
  | 'not_annotation'
  | 'not_applied'

export type ParseResult<T> = { ok: true; value: T } | { ok: false; reason: ParseRejectReason; message: string }

/** 标注行解析结果（脏行按行丢弃，不整批报废；`dropped` 如实上报，不装作没发生）。 */
export interface AnnotationRowsParse {
  rows: ServerAnnotationRow[]
  dropped: number
}

// ---------------------------------------------------------------------------
// 极小工具（不要引入 dayjs / lodash：本模块要在 v8 覆盖率下零依赖跑测试）
// ---------------------------------------------------------------------------

export function normSymbol(symbol: string | null | undefined): string {
  return typeof symbol === 'string' ? symbol.trim().toUpperCase() : ''
}

/**
 * 服务端数字归一化。PG 的 DECIMAL 经 JSON 可能变字符串（仓库既有事故），
 * 故容忍"纯数字字符串"；bool / NaN / Infinity / 负数一律不合格。
 */
export function serverNumber(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const n = Number(value)
    return Number.isFinite(n) ? n : null
  }
  return null
}

function positivePrice(value: unknown): number | null {
  const n = serverNumber(value)
  return n !== null && n > 0 ? n : null
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

// ---------------------------------------------------------------------------
// 解析：GET /copilot/annotations
// ---------------------------------------------------------------------------

/**
 * 严格解析标注列表响应。**fail-closed**：
 *  - 顶层不是数组 → 视为失败（不把对象/字符串当"空列表"，空 ≠ 错）；
 *  - 单行缺 id / label / source 或价位非法 → 只丢这一行（画不了就不画），并计数上报。
 */
export function parseAnnotationsResponse(raw: unknown): ParseResult<AnnotationRowsParse> {
  if (!Array.isArray(raw)) {
    return {
      ok: false,
      reason: 'shape',
      message: '服务端标注响应不是列表（接口契约不符，本次不画线）',
    }
  }
  const rows: ServerAnnotationRow[] = []
  let dropped = 0
  const seen = new Set<number>()
  for (const item of raw) {
    const row = parseAnnotationRow(item)
    if (!row || seen.has(row.id)) {
      dropped += 1
      continue
    }
    seen.add(row.id)
    rows.push(row)
  }
  return { ok: true, value: { rows, dropped } }
}

function parseAnnotationRow(item: unknown): ServerAnnotationRow | null {
  if (!item || typeof item !== 'object' || Array.isArray(item)) return null
  const raw = item as Record<string, unknown>
  const id = serverNumber(raw.id)
  if (id === null || !Number.isInteger(id) || id <= 0) return null
  const label = nonEmptyString(raw.label)
  if (!label) return null
  const source = raw.source === 'ai' || raw.source === 'user' ? raw.source : null
  if (!source) return null
  const price = raw.price === null || raw.price === undefined ? null : positivePrice(raw.price)
  const ts = nonEmptyString(raw.ts)
  // 价位与时间点都没有 → 这行没有可落图的位置；价位写了但非法 → 数据脏，都不画。
  if (price === null && raw.price !== null && raw.price !== undefined) return null
  if (price === null && !ts) return null
  const color: ServerAnnotationRow['color'] =
    raw.color === 'up' || raw.color === 'down' ? raw.color : 'other'
  return {
    id,
    price,
    ts,
    label: label.slice(0, 64),
    note: nonEmptyString(raw.note),
    color,
    source,
    createdAt: typeof raw.created_at === 'string' ? raw.created_at : '',
  }
}

/**
 * 行的视觉方向 → 支撑/压力。
 *
 * **这是服务端字段的确定性映射，不是猜价位**：
 *  - label 以「支撑」/「阻力」「压力」开头 → 直接取（情景矩阵写的就是「支撑位」「阻力位」）；
 *  - 否则看 color（服务端 propose 里 support→up、resist→down）；
 *  - 两者都不给 → 不画（返回 null，不做"默认支撑"这种补全）。
 */
export function kindOfRow(row: ServerAnnotationRow): 'support' | 'pressure' | null {
  const label = row.label.trim()
  if (label.startsWith('支撑')) return 'support'
  if (label.startsWith('阻力') || label.startsWith('压力')) return 'pressure'
  if (row.color === 'up') return 'support'
  if (row.color === 'down') return 'pressure'
  return null
}

/**
 * 服务端行 → 价位线（价位/标签/标的全部来自入参与服务端行，无一来自本地声明）。
 * 没有价位（时间点标注）、方向不可判、标的与请求不符的行一律跳过。
 */
export function annotationLinesFromRows(
  rows: ServerAnnotationRow[],
  request: AnnotationRequest,
): ServerAnnotationLine[] {
  const symbol = normSymbol(request.symbol)
  const period = (request.period || '1d').trim()
  if (!symbol) return []
  const out: ServerAnnotationLine[] = []
  const seen = new Set<string>()
  for (const row of rows) {
    if (row.price === null) continue
    const kind = kindOfRow(row)
    if (!kind) continue
    const key = `${row.id}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push({
      annotationId: row.id,
      price: row.price,
      kind,
      label: row.label,
      source: row.source,
      symbol,
      period,
    })
  }
  return out.sort((a, b) => a.price - b.price)
}

// ---------------------------------------------------------------------------
// 解析：POST /copilot/actions/{id}/apply
// ---------------------------------------------------------------------------

/**
 * 严格解析确认响应。**只有 kind='annotation' 且 status='applied' 才算"已确认"**：
 * pending / rejected / rolled_back 的响应一律拒（这正是"未确认不能上图"的那道闸）。
 */
export function parseApplyResponse(raw: unknown): ParseResult<AppliedAnnotationAction> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { ok: false, reason: 'shape', message: '确认响应不是对象' }
  }
  const obj = raw as Record<string, unknown>
  const actionId = serverNumber(obj.id)
  if (actionId === null || !Number.isInteger(actionId) || actionId <= 0) {
    return { ok: false, reason: 'field', message: '确认响应缺少合法的动作 id' }
  }
  if (obj.kind !== 'annotation') {
    return { ok: false, reason: 'not_annotation', message: `动作类型是 ${String(obj.kind)}，不是图表标注` }
  }
  if (obj.status !== 'applied') {
    return { ok: false, reason: 'not_applied', message: `动作状态是 ${String(obj.status)}，不是已应用` }
  }
  const payload = obj.payload
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { ok: false, reason: 'field', message: '确认响应缺少 payload' }
  }
  const p = payload as Record<string, unknown>
  const symbol = normSymbol(nonEmptyString(p.symbol))
  const label = nonEmptyString(p.label)
  const price = positivePrice(p.price)
  if (!symbol || !label || price === null) {
    return { ok: false, reason: 'field', message: '确认响应的标注 payload 缺 symbol/label/price' }
  }
  const period = nonEmptyString(p.period) ?? '1d'
  const market = nonEmptyString(p.market) ?? 'CN'
  const origin = nonEmptyString(p.source) ?? 'unknown'
  return { ok: true, value: { actionId, symbol, market, period, label, price, origin } }
}

/**
 * 用确认响应**核对**服务端标注行：找到一条（服务端自己给的）label+价位都相同的 ai 标注，
 * 这才叫"这条线对应我刚确认的那次动作"。
 *
 * 找不到就返回失败 —— 常见原因是周期不同（apply 默认 1d，看的是 5m），
 * 此时**不画**并如实说明，而不是拿 apply 响应里的价位直接画（那仍是"绕过读取接口"）。
 */
export function corroborateWithApply(
  applied: AppliedAnnotationAction,
  rows: ServerAnnotationRow[],
  request: AnnotationRequest,
): ParseResult<ServerAnnotationLine[]> {
  if (normSymbol(request.symbol) !== applied.symbol) {
    return { ok: false, reason: 'field', message: '确认的标的与当前 K 线不一致，不画线' }
  }
  const period = (request.period || '1d').trim()
  if (period !== applied.period) {
    return { ok: false, reason: 'field', message: `确认的周期是 ${applied.period}，当前是 ${period}，不画线` }
  }
  const matched = rows.filter(
    (row) =>
      row.source === 'ai' &&
      row.price !== null &&
      Math.abs(row.price - applied.price) < 0.005 &&
      row.label.trim() === applied.label,
  )
  if (matched.length === 0) {
    return {
      ok: false,
      reason: 'field',
      message: '已确认，但服务端标注读回里找不到对应记录（可能已撤销或周期不同），本次未画线',
    }
  }
  const lines = annotationLinesFromRows(matched, request).map((line) => ({
    ...line,
    actionId: applied.actionId,
  }))
  if (lines.length === 0) {
    return { ok: false, reason: 'field', message: '服务端标注记录没有可画的价位，本次未画线' }
  }
  return { ok: true, value: lines }
}

// ---------------------------------------------------------------------------
// 凭证标记（WeakSet 不是"安全凭证"，是"这份数组确实由本模块从服务端响应构造"的机器断言）
// ---------------------------------------------------------------------------

const verifiedServerLineArrays = new WeakSet<object>()

/**
 * 标记一组价位线"由服务端响应解析而来"。
 * 只有本模块的构造路径会调用它；调用方**不得**用它给自造数字镀金。
 */
export function markServerVerified(lines: ServerAnnotationLine[]): ServerAnnotationLine[] {
  verifiedServerLineArrays.add(lines)
  return lines
}

/** 是否为本模块从服务端响应构造出的数组（跨模块唯一判定入口）。 */
export function isServerVerifiedLines(value: unknown): value is ServerAnnotationLine[] {
  return typeof value === 'object' && value !== null && verifiedServerLineArrays.has(value as object)
}

// ---------------------------------------------------------------------------
// 请求 URL（唯一构造点：别在页面里拼字符串，市场族口径与后端保持一致）
// ---------------------------------------------------------------------------

/**
 * 组装读取 URL（`fetchAPI` 自带 `/api` 前缀，这里**不要**再写 `/api`）。
 *
 * market 原样透传：后端只对 A 股族（CN/SH/SZ/BJ）做归并，其它市场按等值匹配。
 */
export function annotationsUrl(request: AnnotationRequest): string {
  const symbol = normSymbol(request.symbol)
  const market = (request.market || 'CN').toUpperCase()
  const period = (request.period || '1d').trim()
  return `/copilot/annotations?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(
    market,
  )}&period=${encodeURIComponent(period)}`
}

/** 当前会话里"我确认过"的标注 id 是否命中服务端行（跨账号/跨标的天然隔离）。 */
export function pickConfirmedRows(
  rows: ServerAnnotationRow[],
  ids: readonly number[],
): ServerAnnotationRow[] {
  if (ids.length === 0) return []
  const wanted = new Set(ids)
  return rows.filter((row) => wanted.has(row.id))
}
