/**
 * 服务端标注 → 图表价位线：**唯一允许把响应变成"可画线"的模块**（2026-10-28 P2 四收口）。
 *
 * ## 这一版为什么存在（安全复审驳回 3ceb290f 的四条红线）
 *
 * 上一版把"能不能画"交给两层客户端自证：会话存储里的标注 id 信封 + `WeakSet` 标记。
 * 两者都可伪造/可绕过，而且读取接口不回显属主与目标 —— 于是出现：
 *
 *  - `KlineChart` 自己拉 `/copilot/annotations` 把**全部行**（含 `user_id IS NULL` 的全局行、
 *    未确认行）画一遍，页面再把另一份画上去 ⇒ 同一价位两条线，未确认也上图；
 *  - `apply` 响应没有 `annotation_id`，只能靠 `label + price` 猜"这条线是不是我刚确认的"；
 *  - 响应没有 symbol/market/period/owner，标的/周期/账号全靠"请求—响应配对"脑补；
 *    而 000001 同时是上证指数与平安银行，market 又被按 A 股族归并，根本无法区分。
 *
 * 现在后端（同一提交）把身份与目标回显出来，前端只做**逐字核对**：
 *
 *     GET /copilot/annotations?symbol&market&period&market_strict=1&mine_only=1&action_linked=1[&ids=]
 *       → {id, symbol, market, period, user_id, action_id, price, ts, label, note, color, source, created_at}
 *
 *     POST /copilot/actions/{id}/apply
 *       → {id, kind:'annotation', status:'applied', annotation_id,
 *          annotation:{id,symbol,market,period,price,ts,label,color,source}}
 *
 * 可画线的**充要条件**（任一不满足 → 不画，并说明原因）：
 *
 *  1. 行拿在我自己名下（`user_id === 我的账号`）；全局行（null）一律不画；
 *  2. 行有确认动作（`action_id != null`）；没有确认动作的行不是"我确认的"；
 *  3. 行的 symbol/market/period 与当前 K 线**逐字相等**（严格模式请求，不做市场族归并）；
 *  4. 标的类型可判定：指数图不画；`000001` 这类指数/个股同号代码，后端没有 instrument 列
 *     ⇒ **默认拒画**（见 {@link targetRefusal}）；
 *  5. 价位是正的有限数、label 非空、方向（支撑/压力）由服务端字段确定性映射。
 *
 * 客户端不提供任何"我说的算"的凭证：没有 WeakSet、没有本地价位信封。价位、标签、
 * 标的、周期、属主、动作 id 全部逐字来自服务端响应；能伪造的只有"请求哪个 id"，
 * 而服务端按 JWT 过滤、按 id 精确返回（他人的行精确指定 id 也看不到）。
 */

// ---------------------------------------------------------------------------
// 后端契约（可执行事实表：UI 文案与测试都读它，别在别处再抄一遍）
// ---------------------------------------------------------------------------

export const SERVER_ANNOTATION_CONTRACT = {
  /** 读取接口（JWT 作用域内；严格模式参数见下） */
  readPath: 'GET /copilot/annotations',
  /** 确认接口（服务端按 user_id 过滤，非本人 400） */
  applyPath: 'POST /copilot/actions/{id}/apply',
  /** 响应行是否带 CopilotAction id（`copilot_actions.annotation_id` 反查）—— 是 */
  actionIdInRow: true,
  /** 响应行是否带回属主（`user_id`；null = 全局行）—— 是 */
  ownerScopeInRow: true,
  /** 响应行是否回显 symbol/market/period —— 是 */
  echoesRequestKeys: true,
  /** 严格模式：`market_strict=1` 不做 A 股族归并 */
  strictMarketParam: 'market_strict',
  /** 严格模式：`mine_only=1` 只返回本人行（不含 user_id IS NULL） */
  mineOnlyParam: 'mine_only',
  /** 严格模式：`action_linked=1` 只返回能对回本人确认动作的行 */
  actionLinkedParam: 'action_linked',
  /** 精确身份读取：`ids=1,2` */
  idsParam: 'ids',
  /** 是否有撤销/删除图注的服务端接口 —— **false**（rollback 对 annotation 返回 400） */
  undoSupported: false,
  /** 未确认的 pending 提议会不会出现在读取接口里 —— **false**（只查 KlineAnnotation） */
  pendingListed: false,
  /** 表里是否有"标的类型（个股/指数）"列 —— **false** ⇒ 同号代码只能默认拒画 */
  instrumentInRow: false,
  /** 标的族：后端把 A 股市场代码按一组处理（严格模式关闭时的行为） */
  aShareMarkets: ['CN', 'SH', 'SZ', 'BJ'] as const,
} as const

/** 图表标的类型（KlineChart 的 `instrument` prop 口径）。 */
export type AnnotationInstrument = 'stock' | 'index'

/**
 * 一次读取的**精确目标**。key 里的每个字段都参与隔离：
 * 换账号 / 换标的 / 换市场 / 换周期 / 换个股↔指数，都是不同的 key。
 */
export interface AnnotationTarget {
  /** 账号（JWT `sub`）；空串 = 未登录（不请求、不画） */
  owner: string
  instrument: AnnotationInstrument
  symbol: string
  market: string
  period: string
}

export function targetKey(target: AnnotationTarget): string {
  return [
    target.owner,
    target.instrument,
    normSymbol(target.symbol),
    (target.market || '').toUpperCase(),
    (target.period || '1d').trim(),
  ].join('|')
}

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

/** 服务端 `GET /copilot/annotations` 的一行（已严格校验后的形状）。 */
export interface ServerAnnotationRow {
  /** KlineAnnotation.id —— 唯一可追溯的服务端标识 */
  id: number
  symbol: string
  market: string
  period: string
  /** 行属主；null = 全局行（不是"我确认的"） */
  ownerId: string | null
  /** 本人确认动作 id；null = 没有确认动作（历史/人工/他人） */
  actionId: number | null
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

/** 能画到 K 线上的价位线（每个字段都能对回服务端行）。 */
export interface ServerAnnotationLine {
  annotationId: number
  actionId: number
  price: number
  kind: 'support' | 'pressure'
  label: string
  symbol: string
  market: string
  period: string
  ownerId: string
}

/** apply 响应里被证实"已确认"的标注动作（新契约：带 annotation_id + 落库明细）。 */
export interface AppliedAnnotation {
  actionId: number
  annotationId: number
  symbol: string
  market: string
  period: string
  label: string
  price: number
  ts: string | null
}

export type ParseRejectReason =
  | 'shape'
  | 'field'
  | 'not_annotation'
  | 'not_applied'
  | 'no_annotation_id'
  | 'mismatch'

export type ParseResult<T> =
  | { ok: true; value: T }
  | { ok: false; reason: ParseRejectReason; message: string }

/** 标注行解析结果（脏行按行丢弃，不整批报废；`dropped` 如实上报）。 */
export interface AnnotationRowsParse {
  rows: ServerAnnotationRow[]
  dropped: number
}

// ---------------------------------------------------------------------------
// 极小工具（不要引入 dayjs / lodash：本模块要在没网的环境里跑测试）
// ---------------------------------------------------------------------------

export function normSymbol(symbol: string | null | undefined): string {
  return typeof symbol === 'string' ? symbol.trim().toUpperCase() : ''
}

/** 服务端数字归一化：容忍纯数字字符串（PG DECIMAL 经 JSON 可能变字符串）。 */
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

function positiveInt(value: unknown): number | null {
  const n = serverNumber(value)
  return n !== null && Number.isInteger(n) && n > 0 ? n : null
}

// ---------------------------------------------------------------------------
// 标的类型：后端没有 instrument 列 ⇒ 同号代码默认拒画
// ---------------------------------------------------------------------------

/**
 * A 股**指数**代码前缀：深证系列 399、通达信板块 880/881、上证综指 999999。
 * 标注行没有 instrument 字段，这些前缀只能整体拒画。
 */
const INDEX_CODE_PREFIXES = ['399', '880', '881', '999']

/**
 * 与深市个股同号的指数代码（000xxx）。**这份名单就是"后端无法保证"的那部分**：
 * 上证/中证系列指数与深市主板股票都用 000xxx，命中的代码无法从标注行证明属于哪一类。
 * 新增指数页时往这里加，不要放宽成"000xxx 全部拒画"（那会误伤万科等正常个股）。
 */
export const INDEX_CODE_COLLISIONS: readonly string[] = [
  '000001', // 上证指数 / 平安银行(SZ000001)
  '000010', // 上证180 / 美丽生态(SZ000010)
  '000016', // 上证50 / 深康佳A(SZ000016)
  '000300', // 沪深300（无法从行证明是"指数"）
  '000688', // 科创50
  '000852', // 中证1000
  '000905', // 中证500
  '000985', // 中证全指
]

/** 该代码是否属于"指数/个股无法从标注行区分"的一类。 */
export function isInstrumentAmbiguous(symbol: string): boolean {
  const sym = normSymbol(symbol)
  if (!sym) return false
  if (INDEX_CODE_PREFIXES.some((p) => sym.startsWith(p))) return true
  return INDEX_CODE_COLLISIONS.includes(sym)
}

/**
 * 目标级拒画判定（发请求前就能下结论的，一律不发请求、不画、把原因交给界面）。
 *
 *  - 未登录：不发请求（后端 401 会触发全局登出，匿名看 K 线不该被踢）；
 *  - 指数图：AI 标注只来自个股情景矩阵，指数图上没有可证明的归属；
 *  - 同号代码（000001 等）：后端没有 instrument 列，**默认拒画**，不做"大概是股票吧"。
 */
export function targetRefusal(target: AnnotationTarget | null): string | null {
  if (!target) return '未画线：当前视图没有可核对的 K 线标的。'
  if (!target.owner) return '未画线：请先登录（标注按账号隔离读取）。'
  if (target.instrument !== 'stock') {
    return '未画线：指数图不显示个股情景标注（后端标注行不记录标的类型，无法证明归属）。'
  }
  if (isInstrumentAmbiguous(target.symbol)) {
    return `未画线：${normSymbol(target.symbol)} 既可能是指数也可能是个股，后端标注行没有标的类型字段，无法区分，默认不画。`
  }
  return null
}

/** 市场是否匹配（严格模式：等值；非严格：A 股族等价）。 */
export function marketMatches(targetMarket: string, rowMarket: string, strict: boolean): boolean {
  const want = (targetMarket || '').toUpperCase()
  const got = (rowMarket || '').toUpperCase()
  if (strict) return want === got
  if (want === got) return true
  const family = SERVER_ANNOTATION_CONTRACT.aShareMarkets as readonly string[]
  return family.includes(want) && family.includes(got)
}

// ---------------------------------------------------------------------------
// 解析：GET /copilot/annotations
// ---------------------------------------------------------------------------

/**
 * 严格解析标注列表响应。**fail-closed**：
 *  - 顶层不是数组 → 失败（不把对象/字符串当"空列表"，空 ≠ 错）；
 *  - 单行缺 id/label/source/目标回显/属主回显 → 丢这一行并计数：
 *    没有 symbol/market/period/user_id 的行在客户端**无法核对目标**，一律画不了。
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
  const id = positiveInt(raw.id)
  if (id === null) return null
  const label = nonEmptyString(raw.label)
  if (!label) return null
  const source = raw.source === 'ai' || raw.source === 'user' ? raw.source : null
  if (!source) return null
  // 目标回显缺一不可：没有这些字段就无法证明"这行属于当前 K 线"
  const symbol = normSymbol(nonEmptyString(raw.symbol))
  const market = nonEmptyString(raw.market)
  const period = nonEmptyString(raw.period)
  if (!symbol || !market || !period) return null
  // 属主回显：字符串 = 本人/他人；null = 全局行（允许解析，由目标核对拒绝）
  const ownerRaw = raw.user_id
  if (ownerRaw !== null && ownerRaw !== undefined && typeof ownerRaw !== 'string') return null
  const ownerId = typeof ownerRaw === 'string' && ownerRaw.trim() ? ownerRaw.trim() : null
  // 动作关联：null 合法（没有确认动作），但类型必须明确
  const actionRaw = raw.action_id
  let actionId: number | null = null
  if (actionRaw !== null && actionRaw !== undefined) {
    actionId = positiveInt(actionRaw)
    if (actionId === null) return null
  }
  const price = raw.price === null || raw.price === undefined ? null : positivePrice(raw.price)
  const ts = nonEmptyString(raw.ts)
  // 价位写了但非法 → 数据脏；价位与时间点都没有 → 没有可落图的位置
  if (price === null && raw.price !== null && raw.price !== undefined) return null
  if (price === null && !ts) return null
  const color: ServerAnnotationRow['color'] =
    raw.color === 'up' || raw.color === 'down' ? raw.color : 'other'
  return {
    id,
    symbol,
    market,
    period,
    ownerId,
    actionId,
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
 * **服务端字段的确定性映射，不是猜价位**：label 前缀（情景矩阵写的就是「支撑位」「阻力位」），
 * 其次 color（propose 里 support→up、resist→down）；两者都不给 → 不画。
 */
export function kindOfRow(row: ServerAnnotationRow): 'support' | 'pressure' | null {
  const label = row.label.trim()
  if (label.startsWith('支撑')) return 'support'
  if (label.startsWith('阻力') || label.startsWith('压力')) return 'pressure'
  if (row.color === 'up') return 'support'
  if (row.color === 'down') return 'pressure'
  return null
}

/** 单行的核对结果：通过时给出可画线；不通过时给出"为什么"。 */
export type RowVerdict =
  | { ok: true; line: ServerAnnotationLine }
  | { ok: false; reason: 'owner' | 'action' | 'target' | 'price' | 'garbage'; message: string }

/**
 * 单行核对：属主、确认动作、目标、价位、方向，全部逐字比对。
 * 这一层是"能不能上图"的唯一判断点 —— 顺序也影响文案：先身份，后目标。
 */
export function verifyRow(row: ServerAnnotationRow, target: AnnotationTarget): RowVerdict {
  const sym = normSymbol(target.symbol)
  const period = (target.period || '1d').trim()
  if (row.source !== 'ai') {
    return { ok: false, reason: 'garbage', message: `标注 #${row.id} 不是 AI 标注，不画。` }
  }
  if (row.ownerId === null) {
    return { ok: false, reason: 'owner', message: `标注 #${row.id} 是全局行（无属主），不画。` }
  }
  if (row.ownerId !== target.owner) {
    return { ok: false, reason: 'owner', message: `标注 #${row.id} 属于其他账号，不画。` }
  }
  if (row.actionId === null) {
    return {
      ok: false,
      reason: 'action',
      message: `标注 #${row.id} 没有对应的确认动作（不是用户确认画上去的），不画。`,
    }
  }
  if (row.symbol !== sym || !marketMatches(target.market, row.market, true) || row.period !== period) {
    return {
      ok: false,
      reason: 'target',
      message: `标注 #${row.id} 的标的/市场/周期（${row.symbol}/${row.market}/${row.period}）与当前 K 线（${sym}/${target.market}/${period}）不一致，不画。`,
    }
  }
  if (row.price === null) {
    return { ok: false, reason: 'price', message: `标注 #${row.id} 没有价位（时间点标注），不上图。` }
  }
  const kind = kindOfRow(row)
  if (!kind) {
    return { ok: false, reason: 'garbage', message: `标注 #${row.id} 无法判断支撑/阻力，不画。` }
  }
  return {
    ok: true,
    line: {
      annotationId: row.id,
      actionId: row.actionId,
      price: row.price,
      kind,
      label: row.label,
      symbol: row.symbol,
      market: row.market,
      period: row.period,
      ownerId: row.ownerId,
    },
  }
}

/** 核对结果汇总（`lines` 可画；`rejected` 为逐条拒绝原因，如实上报）。 */
export interface LinesVerdict {
  lines: ServerAnnotationLine[]
  rejected: string[]
  /** 同向同价的重复行被合并的条数（同一价位只画一条，避免图上两条重合虚线） */
  merged: number
}

/**
 * 行集合 → 可画线集合。逐条过 {@link verifyRow}，再按 (方向, 价位) 去重：
 * 同一价位可能是两次确认（连点/历史重复），图上只保留一条（id 较大的那条，即较新的确认）。
 */
export function linesForTarget(rows: ServerAnnotationRow[], target: AnnotationTarget): LinesVerdict {
  const rejected: string[] = []
  const byLevel = new Map<string, ServerAnnotationLine>()
  let merged = 0
  for (const row of [...rows].sort((a, b) => a.id - b.id)) {
    const verdict = verifyRow(row, target)
    if (!verdict.ok) {
      rejected.push(verdict.message)
      continue
    }
    const key = `${verdict.line.kind}:${Math.round(verdict.line.price * 100)}`
    const prev = byLevel.get(key)
    if (prev) {
      merged += 1
      // 两条都来自服务端、价位一致，保留较新的确认（id 大）以便溯源
      if (verdict.line.annotationId > prev.annotationId) byLevel.set(key, verdict.line)
      continue
    }
    byLevel.set(key, verdict.line)
  }
  const lines = [...byLevel.values()].sort((a, b) => a.price - b.price)
  return { lines, rejected, merged }
}

// ---------------------------------------------------------------------------
// 解析：POST /copilot/actions/{id}/apply
// ---------------------------------------------------------------------------

/**
 * 严格解析确认响应。**只认新契约**：
 *  - `kind='annotation'` 且 `status='applied'`；
 *  - 必须带 `annotation_id`（真实落库的 `kline_annotations.id`）与 `annotation` 明细。
 *
 * 旧响应（没有 `annotation_id`）**不做 label+price 反查**：那正是复审驳回的
 * "用散文对账当关联"。没有 id 就明确失败，界面如实说"无法精确关联"。
 */
export function parseApplyResponse(raw: unknown): ParseResult<AppliedAnnotation> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { ok: false, reason: 'shape', message: '确认响应不是对象' }
  }
  const obj = raw as Record<string, unknown>
  const actionId = positiveInt(obj.id)
  if (actionId === null) {
    return { ok: false, reason: 'field', message: '确认响应缺少合法的动作 id' }
  }
  if (obj.kind !== 'annotation') {
    return {
      ok: false,
      reason: 'not_annotation',
      message: `动作类型是 ${String(obj.kind)}，不是图表标注`,
    }
  }
  if (obj.status !== 'applied') {
    return { ok: false, reason: 'not_applied', message: `动作状态是 ${String(obj.status)}，不是已应用` }
  }
  const annotationId = positiveInt(obj.annotation_id)
  if (annotationId === null) {
    return {
      ok: false,
      reason: 'no_annotation_id',
      message:
        '确认响应没有 annotation_id（后端契约未升级），无法把这次确认与图上某一条精确关联',
    }
  }
  const detail = obj.annotation
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) {
    return { ok: false, reason: 'field', message: '确认响应缺少 annotation 落库明细' }
  }
  const d = detail as Record<string, unknown>
  if (positiveInt(d.id) !== annotationId) {
    return { ok: false, reason: 'mismatch', message: '确认响应的 annotation_id 与明细 id 不一致' }
  }
  const symbol = normSymbol(nonEmptyString(d.symbol))
  const market = nonEmptyString(d.market)
  const period = nonEmptyString(d.period)
  const label = nonEmptyString(d.label)
  const price = positivePrice(d.price)
  if (!symbol || !market || !period || !label || price === null) {
    return {
      ok: false,
      reason: 'field',
      message: '确认响应的标注明细缺 symbol/market/period/label/price',
    }
  }
  const ts = nonEmptyString(d.ts)
  return { ok: true, value: { actionId, annotationId, symbol, market, period, label, price, ts } }
}

/** apply 响应 → 精确目标（标的/市场/周期取自服务端落库明细，属主取自身份）。 */
export function targetOfApplied(applied: AppliedAnnotation, owner: string): AnnotationTarget {
  return {
    owner,
    instrument: 'stock',
    symbol: applied.symbol,
    market: applied.market,
    period: applied.period,
  }
}

/**
 * 用**标注 id** 把"服务端读回的那一行"与"刚才的确认"对上，再核对内容是否自相矛盾。
 *
 * 关联唯一依据 = `annotation_id`（服务端在同一事务里插入并回传）。label/price/ts 只做
 * **一致性核对**：读回的行与确认响应说的内容不一致 → 契约被破坏，拒画并说清哪一项不同。
 */
export function verifyAppliedRow(
  applied: AppliedAnnotation,
  rows: ServerAnnotationRow[],
  target: AnnotationTarget,
): ParseResult<ServerAnnotationLine> {
  const hits = rows.filter((row) => row.id === applied.annotationId)
  if (hits.length === 0) {
    return {
      ok: false,
      reason: 'mismatch',
      message: `已确认标注 #${applied.annotationId}，但精确读取没有返回这一行（可能已撤销），本次未画线。`,
    }
  }
  if (hits.length > 1) {
    return { ok: false, reason: 'mismatch', message: '精确读取返回了重复 id，本次未画线。' }
  }
  const row = hits[0]
  const verdict = verifyRow(row, target)
  if (!verdict.ok) return { ok: false, reason: 'mismatch', message: verdict.message }
  if (verdict.line.actionId !== applied.actionId) {
    return {
      ok: false,
      reason: 'mismatch',
      message: `标注 #${row.id} 关联的是动作 #${verdict.line.actionId}，不是刚才确认的动作 #${applied.actionId}，未画线。`,
    }
  }
  if (row.label.trim() !== applied.label || Math.abs((row.price as number) - applied.price) >= 0.005) {
    return {
      ok: false,
      reason: 'mismatch',
      message: `标注 #${row.id} 的读回内容（${row.label} ${row.price}）与确认响应（${applied.label} ${applied.price}）不一致，未画线。`,
    }
  }
  if ((row.ts ?? null) !== (applied.ts ?? null)) {
    return {
      ok: false,
      reason: 'mismatch',
      message: `标注 #${row.id} 的时间点与确认响应不一致，未画线。`,
    }
  }
  return { ok: true, value: verdict.line }
}

// ---------------------------------------------------------------------------
// 请求 URL（唯一构造点：别在页面里拼字符串）
// ---------------------------------------------------------------------------

export interface ReadOptions {
  /** 精确身份读取：只取这些标注 id */
  ids?: readonly number[]
  /** 严格市场等值（不做 A 股族归并）；默认开 —— 精确目标必须严格 */
  marketStrict?: boolean
  /** 只返回本人行（不含 user_id IS NULL）；默认开 */
  mineOnly?: boolean
  /** 只返回能对回本人确认动作的行；默认开 */
  actionLinked?: boolean
}

/**
 * 组装读取 URL（`fetchAPI` 自带 `/api` 前缀，这里**不要**再写 `/api`）。
 *
 * 默认就是严格模式：目标核对必须能证明"这行属于这只票、这个周期、这个账号"。
 * 关闭严格参数只在诊断/兼容场景显式传入。
 */
export function annotationsUrl(target: AnnotationTarget, options: ReadOptions = {}): string {
  const symbol = normSymbol(target.symbol)
  const market = (target.market || 'CN').toUpperCase()
  const period = (target.period || '1d').trim()
  const params = new URLSearchParams({ symbol, market, period })
  if (options.marketStrict !== false) params.set(SERVER_ANNOTATION_CONTRACT.strictMarketParam, '1')
  if (options.mineOnly !== false) params.set(SERVER_ANNOTATION_CONTRACT.mineOnlyParam, '1')
  if (options.actionLinked !== false) params.set(SERVER_ANNOTATION_CONTRACT.actionLinkedParam, '1')
  const ids = (options.ids ?? []).filter((id) => Number.isInteger(id) && id > 0)
  if (ids.length > 0) params.set(SERVER_ANNOTATION_CONTRACT.idsParam, ids.join(','))
  return `/copilot/annotations?${params.toString()}`
}
