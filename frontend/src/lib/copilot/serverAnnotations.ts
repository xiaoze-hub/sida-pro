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
 *  3. 行的 symbol/period 与当前 K 线逐字相等；market 按**可证明的同一标的**比对
 *     （`provableMarkets`：CN 页 ↔ SH/SZ/BJ 行可证，指数/个股同号不互证）；
 *  4. 标的类型可判定：指数图不画；`000xxx` 整段（上证系列指数与深市主板股票同号）与
 *     纯指数前缀，后端没有 instrument 列 ⇒ **默认拒画**（见 {@link targetRefusal}）；
 *  5. 价位是正的有限数、label 非空、方向（支撑/压力）由服务端字段确定性映射。
 *
 * 客户端不提供任何"我说的算"的凭证：没有 WeakSet、没有本地价位信封。价位、标签、
 * 标的、周期、属主、动作 id 全部逐字来自服务端响应；能伪造的只有"请求哪个 id"，
 * 而服务端按 JWT 过滤、按 id 精确返回（他人的行精确指定 id 也看不到）。
 */

import { dayOnlyDate, type KlineAnnotationMarker } from '@sida/biz-ui/klineEvents'

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
  /** 严格模式：`market_strict=1` = 只返回能**证明**属于同一标的的市场（见 {@link provableMarkets}） */
  strictMarketParam: 'market_strict',
  /** 严格模式：`mine_only=1` 只返回本人行（不含 user_id IS NULL） */
  mineOnlyParam: 'mine_only',
  /** 严格模式：`action_linked=1` 只返回能对回本人确认动作的行 */
  actionLinkedParam: 'action_linked',
  /** 精确身份读取：`ids=1,2` */
  idsParam: 'ids',
  /**
   * 撤销接口：`DELETE /copilot/annotations/{id}?reason=`（2026-10-29 后端新增）。
   * 只撤**本人已确认**的标注：他人 404（与不存在同句，不泄露他人数据）、
   * 无主（`user_id IS NULL`）409、无确认动作 409、编号复用/并发冲突 409。
   */
  revokePath: 'DELETE /copilot/annotations/{id}',
  /** 撤销成功的状态字面量（前端只认这个值，不按 `ok` 猜） */
  revokedStatus: 'revoked',
  /** 撤销成功后确认动作的终态（动作没转终态 ⇒ 撤销没走完） */
  revokedActionStatus: 'rolled_back',
  /**
   * 是否有撤销/删除图注的服务端接口 —— **true**（`DELETE /copilot/annotations/{id}`）。
   * 2026-10-29 之前是 false（rollback 对 annotation 直接 400、没有删除接口），
   * 服务端补上撤销入口后此事实必须跟着翻；界面上的"撤销不可用"文案由它派生。
   */
  undoSupported: true,
  /** 未确认的 pending 提议会不会出现在读取接口里 —— **false**（只查 KlineAnnotation） */
  pendingListed: false,
  /** 表里是否有"标的类型（个股/指数）"列 —— **false** ⇒ 同号代码只能默认拒画 */
  instrumentInRow: true,
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
  instrument?: AnnotationInstrument | null
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

/**
 * 能画到 K 线上的**纯时间点标注**（2026-10-29）。
 *
 * 与 {@link ServerAnnotationLine} 是**两条并列通道**（不是"没有价位的线"）：
 *  - 价位线 = `price` 有值 → 画水平虚线；
 *  - 时间点 = `price` 为 null、`ts` 是**日粒度** → 画该日 K 线上的 marker。
 *
 * 每个字段都能对回服务端行：`annotationId`（`kline_annotations.id`）、
 * `actionId`（本人确认动作）、`date`（服务端 `ts`）、`label`/`tone`（服务端字段确定性映射）。
 * 前端**不新增**任何服务端没有的事实：没有价位就不编价位，日期不是日粒度就不画。
 */
export interface ServerAnnotationMarker extends KlineAnnotationMarker {
  symbol: string
  market: string
  period: string
  ownerId: string
}

/** apply 响应里被证实"已确认"的标注动作（新契约：带 annotation_id + 落库明细）。 */
export interface AppliedAnnotation {
  actionId: number
  annotationId: number
  instrument?: AnnotationInstrument | null
  symbol: string
  market: string
  period: string
  label: string
  /** 价位线标注的价位；纯时间点标注为 null（服务端 `price IS NULL`） */
  price: number | null
  ts: string | null
  /**
   * 纯时间点标注的**日粒度**日期（`ts` 归一化后）；价位线为 null。
   * 与 `ts` 的区别：`ts` 是服务端原文（用于逐字核对），`date` 是能落到 K 线上的那个日期。
   */
  date?: string | null
}

export type ParseRejectReason =
  | 'shape'
  | 'field'
  | 'not_annotation'
  | 'not_applied'
  | 'no_annotation_id'
  | 'mismatch'
  | 'not_revoked'

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
 * 指数/个股**同号**代码段（000xxx）：上证/中证指数系列与深市主板股票都占这一段
 * （000001=上证指数/平安银行、000002=A股指数/万科A、000003=B股指数…）。
 *
 * 这里是**整段**而不是一张"已知撞号"名单：名单只能列已知的那几个，000002/000003
 * 这类名单外的同号代码就漏了 —— 而 000xxx 每一段都可能是上证系列指数。标注行
 * 不记录标的类型（{@link SERVER_ANNOTATION_CONTRACT.instrumentInRow} = false），
 * 也没有别的字段能证明，所以默认**整段拒画**，不做"大概是股票吧"的编码猜测。
 *
 * 与后端 `src/harness/actions.py::AMBIGUOUS_SYMBOL_PREFIXES` 同源；
 * `tests/test_copilot_annotation_contract.py` 有一条防漂移用例逐字比对两份前缀表。
 */
const AMBIGUOUS_SYMBOL_PREFIXES = ['000']

/** 代码段本身分不清指数/个股（允许 `.SZ` 这类后缀：行里仍然证明不了标的类型）。 */
const AMBIGUOUS_SYMBOL_RE = new RegExp(
  `^(?:${[...AMBIGUOUS_SYMBOL_PREFIXES, ...INDEX_CODE_PREFIXES].join('|')})\\d{3}(?!\\d)`,
)

/**
 * A 股**个股**代码前缀 → 交易所（与后端 `EXCHANGE_BY_PREFIX` 同源）。
 * 只收能唯一确定交易所的个股前缀：指数前缀、同号代码、未知前缀一律不推导。
 */
const EXCHANGE_BY_PREFIX: readonly (readonly [string, string])[] = [
  ['60', 'SH'],
  ['68', 'SH'],
  ['90', 'SH'],
  ['00', 'SZ'],
  ['20', 'SZ'],
  ['30', 'SZ'],
  ['43', 'BJ'],
  ['83', 'BJ'],
  ['87', 'BJ'],
]

/** 从代码**证明**所属交易所；证明不了返回 null（不猜：猜错就画到另一只标的上）。 */
export function exchangeOf(symbol: string, instrument?: AnnotationInstrument): string | null {
  const sym = normSymbol(symbol)
  if (!/^\d{6}$/.test(sym)) return null
  if (isInstrumentAmbiguous(sym) && !(instrument === 'stock' && sym.startsWith('000'))) return null
  for (const [prefix, exchange] of EXCHANGE_BY_PREFIX) {
    if (sym.startsWith(prefix)) return exchange
  }
  return null
}

/**
 * 该代码是否属于"指数/个股无法从标注行区分"的一类。
 *
 * 000xxx **整段**都算（不是只算已知撞号的那几个）：上证系列指数与深市主板股票共用
 * 这一段，标注行没有类型字段，任何 000xxx 都无法证明自己是哪一类。
 */
export function isInstrumentAmbiguous(symbol: string): boolean {
  const sym = normSymbol(symbol)
  if (!sym) return false
  return AMBIGUOUS_SYMBOL_RE.test(sym)
}

/**
 * 目标级拒画判定（发请求前就能下结论的，一律不发请求、不画、把原因交给界面）。
 *
 *  - 未登录：不发请求（后端 401 会触发全局登出，匿名看 K 线不该被踢）；
 *  - 指数图：AI 标注只来自个股情景矩阵，指数图上没有可证明的归属；
 *  - 同号代码（000xxx 整段）：后端没有 instrument 列，**默认拒画**，不做"大概是股票吧"。
 */
export function targetRefusal(target: AnnotationTarget | null): string | null {
  if (!target) return '未画线：当前视图没有可核对的 K 线标的。'
  if (!target.owner) return '未画线：请先登录（标注按账号隔离读取）。'
  if (target.instrument !== 'stock') {
    return '未画线：指数图不显示个股情景标注（后端标注行不记录标的类型，无法证明归属）。'
  }
  if (INDEX_CODE_PREFIXES.some((prefix) => normSymbol(target.symbol).startsWith(prefix))) {
    return '未画线：指数代码不能作为个股标的类型展示。'
  }
  return null
}

/**
 * 读取端要查的市场集合：**严格模式也按"可证明的同一标的"展开**，不是字符串相等。
 *
 * 与后端 `src/harness/actions.py::provable_markets` 逐条一致（同一张表、同一条规则）：
 *  - 非 A 股族（HK/US/…）：只认自己；
 *  - `CN`（A 股、未指明交易所）：可与代码推导出的那一个交易所互证；
 *  - `SH`/`SZ`/`BJ`：只与自身互证；推导出的交易所正好是它时，`CN` 行也算同一只；
 *  - 推导不出交易所（000xxx 整段、纯指数前缀、非个股前缀）：**不做任何互证**。
 *
 * 为什么必须这样（2026-10-28 安全复审驳回 ①）：提议里写的是交易所（SH/SZ/BJ），
 * 页面只有 `?market=CN`，字符串相等 ⇒ 落库的线在图上永远看不到；而简单做 A 股族归并
 * ⇒ 000001 的上证指数线会画到 000001（平安银行）的个股图上。只有"能证才等"两头都不错。
 */
export function provableMarkets(symbol: string, market: string, instrument?: AnnotationInstrument): string[] {
  const mk = (market || 'CN').trim().toUpperCase()
  const family = SERVER_ANNOTATION_CONTRACT.aShareMarkets as readonly string[]
  if (!family.includes(mk)) return [mk]
  const exchange = exchangeOf(symbol, instrument)
  if (mk === 'CN') return exchange && exchange !== 'CN' ? ['CN', exchange] : ['CN']
  if (exchange === mk) return [mk, 'CN']
  return [mk]
}

/** 市场是否可证明为同一只标的（不做字符串相等的天真比较，也不做族归并）。 */
export function marketMatches(
  targetMarket: string,
  rowMarket: string,
  symbol: string,
  instrument?: AnnotationInstrument,
): boolean {
  const got = (rowMarket || '').trim().toUpperCase()
  return provableMarkets(symbol, targetMarket, instrument).includes(got)
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
    instrument: raw.instrument === 'stock' || raw.instrument === 'index' ? raw.instrument : null,
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

/** 身份/目标门禁（价位线与时间点两条通道共用）。
 *
 * 抽出来的唯一目的：**两条通道不许各写一套身份判断** —— 那正是"价位线核对了属主、
 * 时间点标注忘了核对"这类漏洞的来源。顺序也固定：先身份（来源→属主→确认动作），后目标
 * （代码→标的类型→市场→周期）。
 */
type RowGuard =
  | { ok: true; ownerId: string; actionId: number; sym: string; period: string }
  | { ok: false; reason: 'owner' | 'action' | 'target' | 'garbage'; message: string }

function guardRow(row: ServerAnnotationRow, target: AnnotationTarget): RowGuard {
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
  if (row.symbol !== sym) {
    return {
      ok: false,
      reason: 'target',
      message: `标注 #${row.id} 属于 ${row.symbol}，当前 K 线是 ${sym}，不画。`,
    }
  }
  // 标的类型不可证（000xxx 整段 / 纯指数前缀）⇒ 这一行**任何入口**都不画。
  // 入口层 {@link targetRefusal} 已经拦过一次（连请求都不发），这里再拦一次：
  // 图上的每一行都必须自己经得起这条校验，不能依赖"上游一定先问过"。
  if ((row.instrument && row.instrument !== target.instrument) || (isInstrumentAmbiguous(sym) && row.instrument !== target.instrument)) {
    return {
      ok: false,
      reason: 'target',
      message:
        `标注 #${row.id} 的目标 ${sym} 属于 000xxx/指数代码段，与上证/中证指数系列同号，` +
        '标注行的标的类型缺失或不一致，无法证明是个股还是指数，不画。',
    }
  }
  // 市场按"可证明的同一标的"比对（CN 页 ↔ SH/SZ/BJ 行可证；指数/个股同号不互证），
  // 文案必须说清是**市场**不一致 —— 旧实现把它并进"标的/市场/周期不一致"，
  // 与周期不符混在一起，用户无法判断是该换市场还是该换周期。
  if (!marketMatches(target.market, row.market, sym, row.instrument ?? undefined)) {
    return {
      ok: false,
      reason: 'target',
      message:
        `标注 #${row.id} 的市场（${row.market}）与当前 K 线市场（${target.market}）` +
        '无法证明是同一只标的，不画。',
    }
  }
  if (row.period !== period) {
    return {
      ok: false,
      reason: 'target',
      message: `标注 #${row.id} 落库在 ${row.period} 周期，当前 K 线是 ${period}，不画。`,
    }
  }
  return { ok: true, ownerId: row.ownerId, actionId: row.actionId, sym, period }
}

/**
 * 单行核对：属主、确认动作、目标、价位、方向，全部逐字比对。
 * 这一层是"能不能上图"的唯一判断点 —— 顺序也影响文案：先身份，后目标。
 */
export function verifyRow(row: ServerAnnotationRow, target: AnnotationTarget): RowVerdict {
  const guard = guardRow(row, target)
  if (!guard.ok) return { ok: false, reason: guard.reason, message: guard.message }
  if (row.price === null) {
    // 纯时间点标注**不属于价位线图层**（它走 {@link verifyRowMarker} 的 marker 通道）。
    // 这里如实说"走别的通道"，而不是说"图上没有对应图层"——后者已经不符合事实。
    return {
      ok: false,
      reason: 'price',
      message: `标注 #${row.id} 是纯时间点标注（没有价位），不在价位线图层（按该日 marker 展示）。`,
    }
  }
  const kind = kindOfRow(row)
  if (!kind) {
    return { ok: false, reason: 'garbage', message: `标注 #${row.id} 无法判断支撑/阻力，不画。` }
  }
  return {
    ok: true,
    line: {
      annotationId: row.id,
      actionId: guard.actionId,
      price: row.price,
      kind,
      label: row.label,
      symbol: row.symbol,
      market: row.market,
      period: row.period,
      ownerId: guard.ownerId,
    },
  }
}

/** 时间点标注的单行核对结果。 */
export type MarkerVerdict =
  | { ok: true; marker: ServerAnnotationMarker }
  | { ok: false; reason: 'owner' | 'action' | 'target' | 'price' | 'garbage'; message: string }

/**
 * 单行核对（时间点通道）：身份/目标与价位线**同一套门禁**（{@link guardRow}），
 * 再要求"没有价位 + 有日粒度日期"。
 *
 * 为什么必须拒"带时分的 ts"：图上能定位的只有**某一根** K 线。`2026-10-28 10:00`
 * 落在哪一根（5m/15m/1d）无法证明，贴到最近一根就是假定位 —— 服务端 ts-only 也只接受
 * 日粒度（`actions._normalize_ts`），前端再守一次，防脏行/旧数据。
 */
export function verifyRowMarker(row: ServerAnnotationRow, target: AnnotationTarget): MarkerVerdict {
  const guard = guardRow(row, target)
  if (!guard.ok) return { ok: false, reason: guard.reason, message: guard.message }
  if (row.price !== null) {
    return {
      ok: false,
      reason: 'price',
      message: `标注 #${row.id} 有价位，属于价位线通道，不在时间点图层。`,
    }
  }
  if (!row.ts) {
    return {
      ok: false,
      reason: 'price',
      message: `标注 #${row.id} 既没有价位也没有时间点，图上没有位置可放。`,
    }
  }
  const date = dayOnlyDate(row.ts)
  if (!date) {
    return {
      ok: false,
      reason: 'garbage',
      message:
        `标注 #${row.id} 的时间点（${row.ts}）不是日粒度，无法证明落在哪一根 K 线，` +
        '不画（不给假定位）。',
    }
  }
  return {
    ok: true,
    marker: {
      annotationId: row.id,
      actionId: guard.actionId,
      date,
      label: row.label,
      tone: row.color === 'up' ? 'up' : row.color === 'down' ? 'down' : 'neutral',
      note: row.note,
      symbol: row.symbol,
      market: row.market,
      period: row.period,
      ownerId: guard.ownerId,
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

/** 两条通道的核对汇总（价位线 + 时间点 marker）。 */
export interface MarksVerdict {
  /** 价位线（画水平虚线） */
  lines: ServerAnnotationLine[]
  /** 纯时间点标注（画该日 K 线 marker） */
  markers: ServerAnnotationMarker[]
  /** 逐条拒绝原因（如实上报，界面用来解释"为什么有的行没上图"） */
  rejected: string[]
  mergedLines: number
  mergedMarkers: number
}

/**
 * 行集合 → 两条通道的可画集合（2026-10-29）。
 *
 * 每条行**只走一条通道**：有价位走价位线，没价位但有日粒度时间点走 marker，
 * 两者都没有/都非法一律拒绝。身份与目标核对两条通道共用 {@link guardRow} ——
 * 也就是说"服务端确认动作/属主/标的类型/市场/周期"对 marker 与线是同一把尺子。
 *
 * 去重口径与 {@link linesForTarget} 一致（同一件事的重复确认只画一次，留较新的 id）。
 */
export function marksForTarget(rows: ServerAnnotationRow[], target: AnnotationTarget): MarksVerdict {
  const rejected: string[] = []
  const byLevel = new Map<string, ServerAnnotationLine>()
  const bySlot = new Map<string, ServerAnnotationMarker>()
  let mergedLines = 0
  let mergedMarkers = 0
  for (const row of [...rows].sort((a, b) => a.id - b.id)) {
    if (row.price !== null) {
      const verdict = verifyRow(row, target)
      if (!verdict.ok) {
        rejected.push(verdict.message)
        continue
      }
      const key = `${verdict.line.kind}:${Math.round(verdict.line.price * 100)}`
      const prev = byLevel.get(key)
      if (prev) {
        mergedLines += 1
        if (verdict.line.annotationId > prev.annotationId) byLevel.set(key, verdict.line)
        continue
      }
      byLevel.set(key, verdict.line)
      continue
    }
    const verdict = verifyRowMarker(row, target)
    if (!verdict.ok) {
      rejected.push(verdict.message)
      continue
    }
    const key = `${verdict.marker.date}|${verdict.marker.label}|${verdict.marker.tone}`
    const prev = bySlot.get(key)
    if (prev) {
      mergedMarkers += 1
      if (verdict.marker.annotationId > prev.annotationId) bySlot.set(key, verdict.marker)
      continue
    }
    bySlot.set(key, verdict.marker)
  }
  return {
    lines: [...byLevel.values()].sort((a, b) => a.price - b.price),
    markers: [...bySlot.values()].sort((a, b) =>
      a.date < b.date ? -1 : a.date > b.date ? 1 : a.annotationId - b.annotationId,
    ),
    rejected,
    mergedLines,
    mergedMarkers,
  }
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
  const ts = nonEmptyString(d.ts)
  const instrument = d.instrument === 'stock' || d.instrument === 'index' ? d.instrument : null
  if (symbol && isInstrumentAmbiguous(symbol) && instrument === null) {
    return { ok: false, reason: 'field', message: '确认响应缺少标的类型，无法区分股票与指数' }
  }
  if (!symbol || !market || !period || !label) {
    return {
      ok: false,
      reason: 'field',
      message: '确认响应的标注明细缺 symbol/market/period/label',
    }
  }
  // 价位写了但非法（0/负数/非数字）→ 数据脏，不做"当成时间点标注"的降级
  if (price === null && d.price !== null && d.price !== undefined) {
    return { ok: false, reason: 'field', message: '确认响应的标注价位不是正数（数据非法）' }
  }
  /*
   * 纯时间点标注（2026-10-29）：`price` 为 null 时必须有**日粒度**时间点。
   * 带时分的 ts 无法证明落在哪一根 K 线（5m/15m/1d 都可能），服务端 ts-only 也只收日粒度
   * （`actions._normalize_ts`）；这里再守一次，脏响应直接拒绝，不猜日期。
   */
  let date: string | null = null
  if (price === null) {
    if (!ts) {
      return {
        ok: false,
        reason: 'field',
        message: '确认响应的标注明细既没有价位也没有时间点，图上没有位置可放',
      }
    }
    date = dayOnlyDate(ts)
    if (!date) {
      return {
        ok: false,
        reason: 'field',
        message: `确认响应的时间点（${ts}）不是日粒度，无法证明落在哪一根 K 线`,
      }
    }
  }
  return {
    ok: true,
    value: { actionId, annotationId, instrument, symbol, market, period, label, price, ts, date },
  }
}

/** apply 响应 → 精确目标（标的/市场/周期取自服务端落库明细，属主取自身份）。 */
export function targetOfApplied(applied: AppliedAnnotation, owner: string): AnnotationTarget {
  return {
    owner,
    instrument: applied.instrument ?? 'stock',
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
/**
 * 已对上号、可上图的标注：**价位线**或**时间点 marker**（两条通道并列）。
 *
 * 调用方必须按 `shape` 分支处理 —— 拿线去画 marker（或反过来）就是把一条没有价位的
 * 标注编出一个价位。类型上强制分支，运行期就不会有人图省事。
 */
export type VerifiedAnnotation =
  | { shape: 'line'; line: ServerAnnotationLine }
  | { shape: 'marker'; marker: ServerAnnotationMarker }

export function verifyAppliedRow(
  applied: AppliedAnnotation,
  rows: ServerAnnotationRow[],
  target: AnnotationTarget,
): ParseResult<VerifiedAnnotation> {
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
  if (applied.instrument && row.instrument !== applied.instrument) {
    return { ok: false, reason: 'mismatch', message: '确认响应与读回记录的标的类型不一致，未画线。' }
  }

  /*
   * 纯时间点标注（2026-10-29）：确认响应没有价位 ⇒ 走 marker 通道。
   * 核对口径与价位线一致（同一套 `guardRow`：属主/确认动作/标的/市场/周期），
   * 只额外核对"读回来的是不是同一个日期"—— 日期不一样就是两条不同的事，
   * 画上去等于把别的时间点冒充成刚确认的那一条。
   */
  if (applied.price === null) {
    const verdictM = verifyRowMarker(row, target)
    if (!verdictM.ok) return { ok: false, reason: 'mismatch', message: verdictM.message }
    if (verdictM.marker.actionId !== applied.actionId) {
      return {
        ok: false,
        reason: 'mismatch',
        message: `标注 #${row.id} 关联的是动作 #${verdictM.marker.actionId}，不是刚才确认的动作 #${applied.actionId}，未上图。`,
      }
    }
    if (row.label.trim() !== applied.label) {
      return {
        ok: false,
        reason: 'mismatch',
        message: `标注 #${row.id} 的读回标签（${row.label}）与确认响应（${applied.label}）不一致，未上图。`,
      }
    }
    if (applied.date && verdictM.marker.date !== applied.date) {
      return {
        ok: false,
        reason: 'mismatch',
        message: `标注 #${row.id} 的时间点（${verdictM.marker.date}）与确认响应（${applied.date}）不一致，未上图。`,
      }
    }
    return { ok: true, value: { shape: 'marker', marker: verdictM.marker } }
  }

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
  return { ok: true, value: { shape: 'line', line: verdict.line } }
}

// ---------------------------------------------------------------------------
// 解析：DELETE /copilot/annotations/{id}
// ---------------------------------------------------------------------------

/** 撤销成功时服务端回传的可核对事实（都是被删那一行的快照）。 */
export interface RevokedAnnotation {
  annotationId: number
  actionId: number
  symbol: string
  label: string
  reason: string
  revokedAt: string
}

/**
 * 严格解析撤销响应。**fail-closed**：只认"这一条确实被删掉了"的证据。
 *
 *  - `ok === true` 且 `status === 'revoked'`（不接受 `ok:true` 配别的状态）；
 *  - `action_status === 'rolled_back'`（动作没转终态 = 事务没走完）；
 *  - `annotation_id` 与 `action_id` 都是正整数，且 `annotation_id` 必须等于**本次请求的 id**
 *    （`expectAnnotationId`）：对不上就不是"这一条被删了"，不能拿它去移除图上的线。
 *
 * 为什么要这么严：撤销的界面动作是"把图上这条线删掉"。响应里 id 对不上时若还照删，
 * 结果是"图上少一条、服务端留着另一条"，用户再刷新线又回来 —— 比报错更糟。
 */
export function parseRevokeResponse(
  raw: unknown,
  expectAnnotationId?: number,
): ParseResult<RevokedAnnotation> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return { ok: false, reason: 'shape', message: '撤销响应不是对象' }
  }
  const obj = raw as Record<string, unknown>
  if (obj.ok !== true) {
    return { ok: false, reason: 'not_revoked', message: '撤销响应 ok 不为 true' }
  }
  if (obj.status !== SERVER_ANNOTATION_CONTRACT.revokedStatus) {
    return {
      ok: false,
      reason: 'not_revoked',
      message: `撤销响应状态是 ${String(obj.status)}，不是 ${SERVER_ANNOTATION_CONTRACT.revokedStatus}`,
    }
  }
  const annotationId = positiveInt(obj.annotation_id)
  if (annotationId === null) {
    return { ok: false, reason: 'field', message: '撤销响应缺少合法的 annotation_id' }
  }
  const actionId = positiveInt(obj.action_id)
  if (actionId === null) {
    return { ok: false, reason: 'field', message: '撤销响应缺少合法的 action_id' }
  }
  if (obj.action_status !== SERVER_ANNOTATION_CONTRACT.revokedActionStatus) {
    return {
      ok: false,
      reason: 'not_revoked',
      message:
        `撤销响应里确认动作的终态是 ${String(obj.action_status)}，` +
        `不是 ${SERVER_ANNOTATION_CONTRACT.revokedActionStatus}（撤销没有走完）`,
    }
  }
  if (expectAnnotationId !== undefined && annotationId !== expectAnnotationId) {
    return {
      ok: false,
      reason: 'mismatch',
      message: `撤销响应说的是标注 #${annotationId}，与本次请求的 #${expectAnnotationId} 不一致`,
    }
  }
  return {
    ok: true,
    value: {
      annotationId,
      actionId,
      symbol: normSymbol(nonEmptyString(obj.symbol)),
      label: nonEmptyString(obj.label) ?? '',
      reason: typeof obj.reason === 'string' ? obj.reason : '',
      revokedAt: typeof obj.revoked_at === 'string' ? obj.revoked_at : '',
    },
  }
}

/**
 * 撤销请求 URL（与 {@link annotationsUrl} 同属"唯一构造点"，页面不要再拼字符串）。
 * 非法 id 直接抛错：拼出 `/annotations/NaN` 的请求没有任何服务端能处理。
 */
export function revokeUrl(annotationId: number, reason = ''): string {
  const aid = positiveInt(annotationId)
  if (aid === null) {
    throw new Error(`标注 id 必须是正整数：${String(annotationId)}`)
  }
  const text = reason.trim()
  const query = text ? `?${new URLSearchParams({ reason: text }).toString()}` : ''
  return `/copilot/annotations/${aid}${query}`
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
  const params = new URLSearchParams({ symbol, market, period, instrument: target.instrument })
  if (options.marketStrict !== false) params.set(SERVER_ANNOTATION_CONTRACT.strictMarketParam, '1')
  if (options.mineOnly !== false) params.set(SERVER_ANNOTATION_CONTRACT.mineOnlyParam, '1')
  if (options.actionLinked !== false) params.set(SERVER_ANNOTATION_CONTRACT.actionLinkedParam, '1')
  const ids = (options.ids ?? []).filter((id) => Number.isInteger(id) && id > 0)
  if (ids.length > 0) params.set(SERVER_ANNOTATION_CONTRACT.idsParam, ids.join(','))
  return `/copilot/annotations?${params.toString()}`
}
