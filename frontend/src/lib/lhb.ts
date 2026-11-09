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
 * 同股同原因只留一条(唯一键兜底); 同股不同原因全部保留。
 */
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

    const reason = text(item.reason)
    if (group.reasons.some((r) => r.reason === reason)) continue
    group.reasons.push({
      reason,
      netBuy: num(item.net_buy),
      buyAmt: num(item.buy_amt),
      sellAmt: num(item.sell_amt),
      dealAmt: num(item.deal_amt),
      turnoverPct: num(item.turnover_pct),
    })
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
