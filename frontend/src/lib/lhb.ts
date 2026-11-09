/**
 * 龙虎榜工作区纯函数(2026-11-09)。
 *
 * 抽出来的理由只有一个:**口径必须只有一份**, 且能在没有 DOM 的情况下被钉住。
 *
 *  ① **同股多上榜原因不合并、更不加总** —— `dragon_tiger_events` 唯一键是
 *     `(trade_date, symbol, reason)`, 同一只票同日可能同时命中"日涨幅偏离值达7%"和
 *     "连续三个交易日涨幅偏离值达20%"。东财每行都带该原因口径下的买入/卖出/净额,
 *     把两行相加会把同一笔成交算两遍(实测同值), 所以这里只**分组**: 一个股票一个卡片,
 *     原因逐条列出, 金额逐行原样展示, 加总留给用户自己判断(页面明确写"不做加总")。
 *
 *  ② **金额单位是元**(后端落库未换算; 见 packages/api/src/archive.ts 头注释),
 *     这里只做 null/字符串安全转换, 绝不乘除 1e4 —— 换算只发生在展示层(safeMoney)。
 *
 *  ③ **买卖方向只认 `side` 字段**。不能用"净额 > 0 就是买方"推断: 席位当日可能净卖出
 *     但仍出现在买方榜(卖出回补), 方向是榜单口径, 不是金额符号。
 *
 *  ④ 缺失值一律 null, 由展示层显示 `--`; 营业部名缺失显示"未披露营业部", **不编造机构**。
 */
import type { ArchiveDragonTigerItem, ArchiveLhbSeatItem } from '@sida/api'

/** 原因缺失时的统一占位(不渲染空白单元格)。 */
export const REASON_FALLBACK = '未注明上榜原因'

/**
 * 席位身份声明 —— 页面必须常驻展示。
 *
 * 营业部席位是交易所披露的**成交归属**, 只能说明"哪家营业部买了", 不能反推成"某某游资进场":
 * 同一营业部有大量散户/量化/机构通道, 游资也可能借道任意营业部。把席位名当成"已验证游资"
 * 是最常见的过度解读, 所以这句话是产品口径, 不是装饰。
 */
export const SEAT_IDENTITY_DISCLAIMER =
  '营业部席位只代表交易所披露的成交归属，不等于已验证游资；同一营业部可能包含散户、量化与机构通道。'

/** 营业部名缺失时的占位(不编造机构名)。 */
export const DEPT_FALLBACK = '未披露营业部'

/**
 * 「身份共用的席位称号」—— 交易所披露里这些名字**不指向某一家**具体机构:
 *   · `机构专用` —— 多家机构共用同一个称号, 3 条「机构专用」可能是 3 家不同机构,
 *     也可能只有 1 家重复上榜; 合并成"1 家"是凭空断言, 拆成"3 家"同样是凭空断言;
 *   · `其他` / `未披露营业部` / `未知` —— 根本没说身份。
 * 命中即视为身份不可核实, 只按源记录条数展示, 不并入任何家数。
 *
 * 注: `沪股通专用` / `深股通专用` 不在此列 —— 那是单一通道, 名字本身就唯一。
 */
const GENERIC_SEAT_NAME_RE = /^(机构专用|机构席位|其他|未披露|未知)/

/** 这条记录的身份是否只是共用称号/占位(→ 不可核实)。 */
export function isGenericSeatName(name: unknown): boolean {
  const n = (typeof name === 'string' ? name : '').replace(/\s+/g, '')
  if (!n) return true
  if (n === DEPT_FALLBACK) return true
  return GENERIC_SEAT_NAME_RE.test(n)
}

/**
 * 身份不可核实时的页面说明 —— 只解释"为什么只报条数", 不给任何家数。
 */
export const SEAT_IDENTITY_UNKNOWN_NOTE =
  '本次明细中有席位身份无法核实：交易所披露的「机构专用」等称号由多家机构共用，未披露营业部也不指向具体机构。这些记录只按源记录条数列出，既不合并也不拆分，不作独立家数统计。'

/**
 * 单日榜单的请求上限(与后端 `limit` 默认值同档; 后端夹在 1..5000)。
 *
 * 页面显式传它, 才能判断"是不是刚好取满" —— 取满就**不能**说"这是全榜",
 * 只能说"可能被截断"。不传的话用的是后端默认值, 前端无从判断。
 */
export const LHB_DAY_LIMIT = 2000

/**
 * 覆盖口径: 归档接口**没有官方完整清单**, 但后端(2026-11-09 质量定性)会随日榜返回
 * `quality`(缺口/风险/交叉提示)。页面两件事分开说:
 *   · 截断 = 前端**可证明**的事实(行数触达单次上限);
 *   · 完整性 = 只转述后端定性, 拿不到就是未知 —— 绝不因"有行"就推断完整。
 */
export interface LhbCoverage {
  /** true = 行数触达单次上限, 可能被截断 */
  truncated: boolean
  /** 展示用语气(截断/无覆盖为 danger, 其余 warning) */
  tone: 'warning' | 'danger'
  /** 页面说明文本(含条数与后端定性; 数据日期单独展示, 不混在这句里) */
  text: string
}

/** 后端没给 quality 时的统一说法: 未知, 不猜两边。 */
export const QUALITY_UNKNOWN_LABEL = '覆盖未核实'
export const QUALITY_UNKNOWN_TEXT =
  '覆盖未核实：后端未返回该日质量定性，完整性保持未知；已入库条数不等于当日已披露全量，也不能据此认为榜单残缺。'

export type LhbQualitySeverity = 'ok' | 'caution' | 'missing' | 'danger' | 'unknown'

export interface LhbQualityView {
  /** false = 后端没给可识别的定性; 页面只能显示"未知" */
  known: boolean
  severity: LhbQualitySeverity
  tone: 'success' | 'warning' | 'danger' | 'info'
  /** 页面标签(来自后端, 但"完整"类标签必须经官方核对才允许出现) */
  label: string
  /** 定性说明(后端 note 原文 + 必要的口径补充) */
  text: string
  /** 风险提示(后端 warnings / 早写风险), 逐条展示 */
  risks: string[]
  /** 两表交叉提示(仅提示, 不推完整性) */
  crossCheck: string
  /** 写入窗口 + 发布就绪经验下界 */
  writeWindow: string
  /** 页面是否允许出现"已核对完整"——只有 verified_against_official=true 才为 true */
  completenessClaim: boolean
  rowsTotal: number | null
  rowsInMarket: number | null
}

function obj(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function strArr(v: unknown): string[] {
  if (!Array.isArray(v)) return []
  return v.map((x) => text(x)).filter(Boolean)
}

/**
 * 后端 quality → 展示视图。**唯一允许说"已核对完整"的路径**是
 * `status=complete && complete=true && verified_against_official=true`;
 * 其它情况(尤其后端口头声明 complete 却没核对)一律降级为"不采信", 并把理由写进正文。
 */
export function lhbQualityView(raw: unknown): LhbQualityView {
  const o = obj(raw)
  const status = text(o?.status).toLowerCase()
  if (!o || !status) {
    return {
      known: false,
      severity: 'unknown',
      tone: 'warning',
      label: QUALITY_UNKNOWN_LABEL,
      text: QUALITY_UNKNOWN_TEXT,
      risks: [],
      crossCheck: '',
      writeWindow: '',
      completenessClaim: false,
      rowsTotal: null,
      rowsInMarket: null,
    }
  }

  const verified = o.verified_against_official === true
  const claimsComplete = o.complete === true || o.completeness_claimed === true
  const backendLabel = text(o.label)

  let severity: LhbQualitySeverity
  let tone: LhbQualityView['tone']
  let label: string
  let base: string

  if (status === 'absent') {
    severity = 'missing'
    tone = 'danger'
    label = backendLabel || '无快照'
    base = '后端定性「无快照」：该日归档无行，无覆盖不等于当日没有龙虎榜（可能尚未采集，或当日为非交易日）。'
  } else if (status === 'partial') {
    severity = 'danger'
    tone = 'danger'
    label = backendLabel || '部分覆盖'
    base = '后端定性「部分覆盖」：该日数据存在可证明的缺口，不能当作全量。'
  } else if (status === 'complete') {
    severity = verified ? 'ok' : 'caution'
    tone = verified ? 'success' : 'warning'
    label = verified ? '已核对完整' : '完整未经核对'
    base = verified
      ? '后端定性「已核对完整」：该日榜单已与官方完整清单核对一致。'
      : '后端声称完整，但未与官方完整清单核对：页面不采信该完整性声明。'
  } else if (status === 'unverified') {
    severity = 'caution'
    tone = 'warning'
    label = backendLabel && backendLabel !== '完整' ? backendLabel : '未经核对'
    base = '后端定性「未经核对」：该表为定时回源落库的独立来源，无官方完整清单可核对，不声明完整，也无证据表明残缺。'
  } else {
    severity = 'unknown'
    tone = 'info'
    label = backendLabel || status
    base = `后端定性「${label}」。`
  }

  const note = text(o.note)
  const risks = strArr(o.warnings)
  const early = text(o.early_freeze_note)
  if (early && !risks.some((r) => r.includes(early))) risks.unshift(early)

  // 后端声称完整却未核对: 无论 status 怎么写, 都不许把"完整"当结论放行。
  if (claimsComplete && !verified) {
    label = status === 'complete' ? '完整未经核对' : label
    if (!base.includes('不采信')) base += ' 后端完整性声明未经官方核对，页面不采信。'
    const warn = '完整性声明未经官方核对，不采信该声明。'
    if (!risks.includes(warn)) risks.push(warn)
  }

  // 标签兜底: 只有三项同时成立才允许出现"已核对完整"这类确定性表述。
  if (!(status === 'complete' && claimsComplete && verified) && (label === '完整' || label.includes('已核对完整'))) {
    label = '完整未经核对'
  }

  let textOut = note && status !== 'unverified' && status !== 'absent' ? `${base} ${note}` : base
  const scope = text(o.market_scope_note)
  if (scope) textOut += ` ${scope}`

  const cc = obj(o.cross_check)
  let crossCheck = ''
  if (cc) {
    const daily = num(cc.daily_symbols)
    const onlyEvents = num(cc.only_in_events)
    const onlyDaily = num(cc.only_in_daily)
    const parts: string[] = []
    if (onlyEvents !== null) parts.push(`多 ${onlyEvents} 只`)
    if (onlyDaily !== null) parts.push(`少 ${onlyDaily} 只`)
    if (daily !== null) parts.push(`daily ${daily} 只`)
    const head = parts.length ? `交叉提示：events 相对 daily ${parts.join('、')}；` : '交叉提示：'
    crossCheck = `${head}${text(cc.note) || '两表均非官方完整清单，差异不能判定任何一方完整。'}`
  }

  const fw = text(o.first_write_at)
  const lw = text(o.last_write_at)
  const ready = text(o.publish_ready_after)
  const basis = text(o.publish_ready_basis)
  const winParts: string[] = []
  if (fw || lw) winParts.push(`写入窗口 ${fw || '未知'} → ${lw || '未知'}`)
  if (ready) winParts.push(`发布就绪下界 ${ready}${basis ? `（${basis}）` : '（经验值，非官方发布时间）'}`)
  const writeWindow = winParts.join('；')

  return {
    known: true,
    severity,
    tone,
    label,
    text: textOut,
    risks,
    crossCheck,
    writeWindow,
    completenessClaim: status === 'complete' && claimsComplete && verified,
    rowsTotal: num(o.rows_total),
    rowsInMarket: num(o.rows_in_market),
  }
}

export function lhbCoverage(rows: number, limit: number = LHB_DAY_LIMIT, quality?: unknown): LhbCoverage {
  const n = Number.isFinite(rows) && rows > 0 ? Math.floor(rows) : 0
  const q = lhbQualityView(quality)
  const truncated = n > 0 && n >= limit

  const countLine =
    n === 0
      ? quality === undefined || quality === null
        ? '该日归档库无入库记录，不代表当日没有龙虎榜（可能尚未采集，或当日为非交易日）。'
        : ''
      : `本页展示归档库已入库的 ${n} 条记录。`

  const truncLine = truncated
    ? `本页取到 ${n} 条，已触达单次上限 ${limit} 条，可能被截断；截断部分未被读取，不能当作全榜。`
    : ''

  const text = [countLine, truncLine, q.text].filter(Boolean).join(' ')
  return {
    truncated,
    tone: truncated || q.severity === 'missing' || q.severity === 'danger' ? 'danger' : 'warning',
    text,
  }
}

/**
 * 后端的空榜 `note` 里混着"完整榜单(含 ETF/可转债 6 位码)"这种**无凭据的完整性承诺**
 * (见 `src/web/api/market_archive.py` 的 dragon_tiger 返回值)。原样展示等于替后端担保
 * 全量, 与"覆盖未核实"自相矛盾 —— 这里只保留可核对的分句("空=无覆盖")。
 */
export function archiveNoteText(note: unknown): string {
  return text(note)
    .split(/[;；]/)
    .map((s) => s.trim())
    .filter((s) => s && !s.includes('完整'))
    .join('；')
}

/** 数智参谋一级入口(与 ReportsHub / History 同一个真实地址)。 */
export const COPILOT_PATH = '/ai-copilot'

/**
 * 参谋入口认作**股票锚点**的代码口径 —— 与 `src/pages/AiCopilot.tsx` 的
 * `SYMBOL_RE = /^[03468]\d{5}$/` 完全一致。
 *
 * 为什么前端必须自己判一次: 入口对 `?symbol=` 只是 `if (SYMBOL_RE.test(sym)) setAnchorSymbol(sym)`。
 * ETF(510300/159915…)、可转债(110xxx/113xxx/123xxx/127xxx/128xxx…)都是 6 位但不是股票,
 * 带进去**不会**出现锚点条。交接文案若统一写"会带上锚点", 就是在承诺一个不会发生的状态。
 */
export const COPILOT_ANCHOR_RE = /^[03468]\d{5}$/

export function isCopilotAnchorSymbol(symbol: unknown): boolean {
  return COPILOT_ANCHOR_RE.test(text(symbol))
}

/**
 * 「分析此股龙虎榜」要问参谋的问题 —— 只交代**事实**(代码、名称、交易日)与边界
 * (只引用当日归档、缺数据直说), 不预写任何结论(不写"净买入意味着…")。
 */
export function lhbCopilotQuestion(input: { symbol: string; name?: string; date: string }): string {
  const symbol = text(input.symbol)
  const name = text(input.name)
  const day = formatTradeDate(input.date)
  const who = name ? `${symbol} ${name}` : symbol
  return (
    `分析 ${day} 的龙虎榜：${who}。` +
    '请按上榜原因逐条说明买入额、卖出额与净买额，并列出买方/卖方营业部席位。' +
    '只引用该交易日的归档数据；没有数据就直接说没有，不要推测、不要用其它日期顶替。'
  )
}

/**
 * 拼参谋跳转地址(纯函数, 便于钉住"带了什么、没带什么")。
 *
 * 只用入口**真实生效**的参数(见 `src/pages/AiCopilot.tsx` 的 URL 解析):
 *   · `symbol` → 锚点股票(**只对 A 股代码生效**, 见 COPILOT_ANCHOR_RE);
 *   · `q`      → 预填问题(不自动发送, 需用户自己点发送)。
 * 刻意**不带**:
 *   · `agent` —— 现有配方(premarket/postmarket/chip…)都没有龙虎榜工具, `lhb.daily`
 *     只挂在通用问答的 `query_data_domain` 上; 硬塞一个配方等于把问题路由到没有数据的
 *     参谋, 属于臆造上下文;
 *   · `date`  —— 入口目前对 `?date=` 只是 `void sp.get('date')`, 不进模型上下文;
 *     带上会让人以为参谋已按该日取数。日期写进问题正文, 用户可见可核对。
 *
 * 代码或日期非法 → 返回 ''(调用方不得跳转, 不许跳到一个没有标的的空问答)。
 */
export interface LhbCopilotHandoff {
  url: string
  /** 入口是否真的会把这个代码设为股票锚点(A 股代码才为 true) */
  anchor: boolean
  /** 无法设锚点时的说明(只说入口没认它, 不猜证券类型); 有锚点为空串 */
  anchorNote: string
}

/**
 * 无法设股票锚点时的说明 —— **不猜证券类型**。
 *
 * 旧文案写"属 ETF/可转债等非股票代码": 950/920 段(北交所)与 5xxxxx/1xxxxx 一样不满足
 * 入口的 `^[03468]\d{5}$`, 于是北交所股票被说成"非股票"。前端没有权威的证券类型表,
 * 所以这里只说**可核实的事实**: 入口没把它识别为股票锚点; 至于它是什么, 本页不判断。
 */
export function lhbAnchorNote(symbol: string): string {
  return `${symbol} 未被参谋入口识别为股票锚点（入口按既定代码前缀规则设锚点，本页不判断它的证券类型）。`
}

export function lhbCopilotHandoff(input: { symbol: string; name?: string; date: string }): LhbCopilotHandoff {
  const symbol = text(input.symbol)
  const date = toApiDate(input.date)
  if (!symbol || !date) return { url: '', anchor: false, anchorNote: '' }
  const anchor = isCopilotAnchorSymbol(symbol)
  const params = new URLSearchParams()
  // 入口不会设锚点的代码不传 symbol: 传了只会让"已带锚点"的文案变成假话。
  if (anchor) params.set('symbol', symbol)
  params.set('q', lhbCopilotQuestion({ symbol, name: text(input.name), date }))
  return {
    url: `${COPILOT_PATH}?${params.toString()}`,
    anchor,
    anchorNote: anchor ? '' : lhbAnchorNote(symbol),
  }
}

/** 只要地址的调用方(单测/旧调用)用这个; 口径与 lhbCopilotHandoff 同一份。 */
export function buildLhbCopilotUrl(input: { symbol: string; name?: string; date: string }): string {
  return lhbCopilotHandoff(input).url
}

export interface LhbReasonRow {
  /** 原始上榜原因(已 trim; 空串由 reasonLabel 兜底) */
  reason: string
  /** 净买额(元) */
  netBuy: number | null
  /** 买入额(元) */
  buyAmt: number | null
  /** 卖出额(元) */
  sellAmt: number | null
  /** 成交额(元) */
  dealAmt: number | null
  turnoverPct: number | null
}

export interface LhbStockGroup {
  symbol: string
  name: string
  /** 该股当日数据所属交易日(YYYYMMDD) */
  tradeDate: string
  close: number | null
  changePct: number | null
  /** 逐条上榜原因(同因去重; 不做金额合并) */
  reasons: LhbReasonRow[]
  /** true = 同股多条原因, 页面须显示"不做加总"说明 */
  multipleReasons: boolean
}

export interface LhbSeatRow extends ArchiveLhbSeatItem {
  /** 展示用营业部名(缺失 → DEPT_FALLBACK) */
  deptName: string
  /** 展示用净额(元); 原字段可能为字符串 */
  netAmt: number | null
  /** 展示用买入额(元) */
  buyAmt: number | null
  /** 展示用卖出额(元) */
  sellAmt: number | null
}

function num(v: unknown): number | null {
  if (v === null || v === undefined) return null
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v === 'string') {
    const s = v.trim()
    if (!s) return null
    const n = Number(s)
    return Number.isFinite(n) ? n : null
  }
  return null
}

function text(v: unknown): string {
  return typeof v === 'string' ? v.trim() : v === null || v === undefined ? '' : String(v).trim()
}

export function reasonLabel(reason: string | null | undefined): string {
  return text(reason) || REASON_FALLBACK
}

/** YYYYMMDD / YYYY-MM-DD → '2026-11-06'; 非法值 → '--'(不猜日期)。 */
export function formatTradeDate(v: unknown): string {
  const raw = text(v).replace(/-/g, '')
  if (!/^\d{8}$/.test(raw)) return '--'
  return `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`
}

/** '20261106' / '2026-11-06' → '2026-11-06'(input[type=date] 的值); 非法 → ''。 */
export function toDateInputValue(v: unknown): string {
  const f = formatTradeDate(v)
  return f === '--' ? '' : f
}

/** '2026-11-06' → '20261106'(接口参数口径); 非法 → ''。 */
export function toApiDate(v: unknown): string {
  const raw = text(v).replace(/-/g, '')
  return /^\d{8}$/.test(raw) ? raw : ''
}

/**
 * overview.tables 里 `dragon_tiger_events` 的最新可用日期(YYYYMMDD)。
 * 统计失败/表缺失/空值 → null(页面不得拿"今天"冒充数据日期)。
 */
export function latestDragonTigerDate(
  tables: { table?: string; latest_date?: string | null }[] | null | undefined,
): string | null {
  if (!Array.isArray(tables)) return null
  const hit = tables.find((t) => t && t.table === 'dragon_tiger_events')
  const raw = text(hit?.latest_date).replace(/-/g, '')
  return /^\d{8}$/.test(raw) ? raw : null
}

/**
 * 榜单行 → 按股票分组(保持后端顺序: 日期降序、代码升序, 不重排)。
 * 同股同原因且**整行读数一致**才算重复行(唯一键兜底); 同股不同原因全部保留。
 *
 * 只按 reason 去重是错的: `reason` 为 NULL/空串时唯一键在 SQL 里不去重(多个 NULL 互不相等),
 * 两行金额完全不同却会被折叠成一条 —— 用户看到的净买额就不是任何一行的原值了。金额是逐行
 * 披露的, 折一行等于吞掉一整条读数。
 */
function reasonRowKey(r: LhbReasonRow): string {
  return [r.reason, r.netBuy, r.buyAmt, r.sellAmt, r.dealAmt].map((v) => (v === null ? '' : String(v))).join('|')
}

export function groupLhbBySymbol(items: ArchiveDragonTigerItem[] | null | undefined): LhbStockGroup[] {
  const groups = new Map<string, LhbStockGroup>()
  for (const item of items ?? []) {
    if (!item) continue
    const symbol = text(item.symbol)
    if (!symbol) continue

    let group = groups.get(symbol)
    if (!group) {
      group = {
        symbol,
        name: text(item.name),
        tradeDate: text(item.trade_date),
        close: num(item.close),
        changePct: num(item.change_pct),
        reasons: [],
        multipleReasons: false,
      }
      groups.set(symbol, group)
    }
    if (!group.name && item.name) group.name = text(item.name)

    const reason: LhbReasonRow = {
      reason: text(item.reason),
      netBuy: num(item.net_buy),
      buyAmt: num(item.buy_amt),
      sellAmt: num(item.sell_amt),
      dealAmt: num(item.deal_amt),
      turnoverPct: num(item.turnover_pct),
    }
    const key = reasonRowKey(reason)
    if (group.reasons.some((r) => reasonRowKey(r) === key)) continue
    group.reasons.push(reason)
  }
  for (const group of groups.values()) group.multipleReasons = group.reasons.length > 1
  return [...groups.values()]
}

function seatKey(seat: ArchiveLhbSeatItem): string {
  const uid = text(seat.row_uid)
  if (uid) return uid
  return [
    text(seat.side),
    text(seat.operate_dept_code),
    text(seat.operate_dept_name),
    text(seat.trade_id),
    text(seat.buy_amt),
    text(seat.sell_amt),
    text(seat.reason),
  ].join('|')
}

/** 净额降序, null 排最后(不能把缺失当 0 排到前面)。 */
function byNetDesc(a: LhbSeatRow, b: LhbSeatRow): number {
  if (a.netAmt === null && b.netAmt === null) return 0
  if (a.netAmt === null) return 1
  if (b.netAmt === null) return -1
  return b.netAmt - a.netAmt
}

/** 席位行 → 买方/卖方两组(只认 side; row_uid 去重; 未知 side 丢弃)。 */
export function splitSeats(items: ArchiveLhbSeatItem[] | null | undefined): { buy: LhbSeatRow[]; sell: LhbSeatRow[] } {
  const seen = new Set<string>()
  const buy: LhbSeatRow[] = []
  const sell: LhbSeatRow[] = []
  for (const item of items ?? []) {
    if (!item) continue
    const side = text(item.side)
    if (side !== 'buy' && side !== 'sell') continue
    const key = seatKey(item)
    if (seen.has(key)) continue
    seen.add(key)
    const row: LhbSeatRow = {
      ...item,
      deptName: text(item.operate_dept_name) || DEPT_FALLBACK,
      netAmt: num(item.net_amt),
      buyAmt: num(item.buy_amt),
      sellAmt: num(item.sell_amt),
    }
    ;(side === 'buy' ? buy : sell).push(row)
  }
  buy.sort(byNetDesc)
  sell.sort(byNetDesc)
  return { buy, sell }
}

/**
 * 席位计数口径: **行数 ≠ 独立席位家数**, 而且**家数只有在身份可核实时才存在**。
 *
 * 两层事实分开:
 *  · `rows` —— 源记录条数, 任何时候都可展示, 不需要任何推断;
 *  · `departments` —— 可核实的独立席位数(按席位代码去重, 无代码时按具体营业部全名),
 *    只有 `identityVerified=true` 时才能当"家营业部"展示。
 *
 * `identityVerified=false` 的三种情形(都只报条数 + 身份未知):
 *  ① 有「机构专用」这类**多家机构共用**的称号 —— 合并成 1 家是断言"只有一家",
 *     拆成 N 家是断言"有 N 家", 两者都是凭空的; (复审 2026-11-09 第二轮)
 *  ② 营业部名与席位代码都缺 —— 没有任何身份证据, 之前"各算一家"同样是凭空;
 *  ③ 同一具体营业部名对应多个不同席位代码 —— 名称不足以区分, 计数不可信。
 *
 * 判定只用身份证据(名称/席位代码)。**绝不用金额相等去推同一家** —— 金额相同的两行
 * 可能是两家不同席位恰好同额, 也可能是同一笔成交的两个原因口径, 金额不携带身份信息。
 */
export interface SeatSummary {
  /** 源记录条数(≠ 独立席位数), 恒可展示 */
  rows: number
  /** 可核实的独立席位数; identityVerified=false 时恒为 0 —— 不提供任何可被误读成家数的数字 */
  departments: number
  /** true = 每一行身份都可核实, departments 才能读作"家营业部" */
  identityVerified: boolean
  /** 身份不可核实的记录条数(共用称号 / 名称代码都缺 / 名称冲突) */
  unknownIdentityRows: number
  /** true = 这些行覆盖了多条上榜原因(行数可能被同一营业部重复占位) */
  multipleReasons: boolean
}

export function seatSummary(seats: LhbSeatRow[] | null | undefined): SeatSummary {
  const list = seats ?? []
  const depts = new Set<string>()
  const reasons = new Set<string>()
  // 具体营业部名 → 出现过的席位代码集合, 用来发现"同名多码"的身份冲突
  const codesByName = new Map<string, Set<string>>()
  let unknown = 0
  for (const s of list) {
    if (!s) continue
    const rawName = text(s.operate_dept_name)
    const name = rawName === DEPT_FALLBACK ? '' : rawName
    const code = text(s.operate_dept_code)
    if (isGenericSeatName(name) || (!name && !code)) {
      // 共用称号 / 无任何身份证据: 不并入任何一家, 也不各自算一家
      unknown += 1
    } else if (code) {
      depts.add(`c:${code}`)
      const set = codesByName.get(name || `c:${code}`) ?? new Set<string>()
      set.add(code)
      codesByName.set(name || `c:${code}`, set)
    } else {
      depts.add(`n:${name}`)
      const set = codesByName.get(name) ?? new Set<string>()
      set.add(`n:${name}`)
      codesByName.set(name, set)
    }
    const reason = text(s.reason)
    if (reason) reasons.add(reason)
  }
  // 同名多码 / 同名既有带码行又有无码行 → 名称不足以区分, 计数不可信
  const conflict = [...codesByName.values()].some((codes) => codes.size > 1)
  return {
    rows: list.length,
    departments: unknown > 0 || conflict ? 0 : depts.size,
    // 没有行就没有"身份不可核实"这回事: 空侧照样算已核实(展示只是 0 条记录)
    identityVerified: unknown === 0 && !conflict,
    unknownIdentityRows: unknown,
    multipleReasons: reasons.size > 1,
  }
}

/**
 * 席位计数展示文案(单一份口径, 组件不自己拼)。
 *
 * 身份核实了才写"家营业部"; 核实不了**只写条数 + 身份未知** —— 不写任何家数,
 * 也不说"至少 N 家"这种换个说法的推测。
 */
export function seatCountText(s: SeatSummary): string {
  if (s.rows === 0) return '0 条记录'
  if (s.identityVerified) return `${s.rows} 条记录 · ${s.departments} 家营业部`
  return `${s.rows} 条记录 · 身份未知，不统计独立家数`
}
