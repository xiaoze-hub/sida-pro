/**
 * 数据落库面(archive)只读 API 客户端 —— 龙虎榜工作区专用(2026-11-09)。
 *
 * **契约来源是代码, 不是猜**:
 *  · 路由 `src/web/api/market_archive.py`(前缀即 `/api/archive`, 见 src/web/bootstrap 挂载):
 *    - `GET /dragon-tiger?date&symbol&days&limit` → `{count, note, items}`
 *      参数校验: date 与 symbol 至少给一个(否则 400); date 须 YYYYMMDD; limit 夹在 1..5000。
 *      SQL: `SELECT * FROM dragon_tiger_events`(列见 src/web/migrations.py `_m156`:
 *      trade_date/symbol/market/name/reason/close/change_pct/net_buy/buy_amt/sell_amt/
 *      deal_amt/turnover_pct/free_market_cap/source/created_at)。
 *      唯一键 `(trade_date, symbol, reason)` ⇒ **同股同日可有多条上榜原因**, 每条是独立原始行。
 *    - `GET /lhb-seats?date&symbol&side&days&limit` → `{count, note, items}`
 *      SQL: `SELECT * FROM lhb_seat_details`(列见 migrations `lhb_seat_details`:
 *      row_uid/trade_date/side('buy'|'sell')/symbol/name/reason/operate_dept_code/
 *      operate_dept_name/close/change_pct/accum_amount/accum_volume/buy_amt/sell_amt/
 *      net_amt/rise_probability_3d/change_type/trade_id/total_buy_ratio/total_sell_ratio/
 *      source/created_at)。row_uid = 内容哈希主键(同一营业部同日同股可在不同原因下多行)。
 *    - `GET /overview` → `{tables:[{table,rows,rows_estimated,earliest_date,latest_date}], redis_cache, note}`
 *      用来取 `dragon_tiger_events` 的**最新可用日期**, 让页面默认落在有数据的那一天。
 *
 * **金额单位 = 元**(不是万元): 榜单来自东财 `RPT_DAILYBILLBOARD_DETAILSNEW`(源字段
 * BILLBOARD_NET_AMT/BUY/SELL/DEAL_AMT, 见 packages/marketdata/.../market_flow.py 与
 * DragonTigerItem 注释; 席位 BUY/SELL/NET 同源), 落库未做单位换算。
 * 展示层用 `@/lib/format` 的 safeMoney 系列换算成 万/亿, 前端**不得**再乘/除 1e4。
 *
 * 红线(与后端一致): 无数据就是空 items + note, 不编造; 这里只透传, 不补默认值。
 */
import { fetchAPI } from './client'

/** 龙虎榜榜单原始行(元口径; reason 可能为 null/空串)。 */
export interface ArchiveDragonTigerItem {
  trade_date: string
  symbol: string
  market?: string | null
  name?: string | null
  /** 上榜原因; 同股同日多原因 = 多行(唯一键 trade_date+symbol+reason)。 */
  reason?: string | null
  close?: number | string | null
  change_pct?: number | string | null
  /** 龙虎榜净买额(元) */
  net_buy?: number | string | null
  /** 龙虎榜买入额(元) */
  buy_amt?: number | string | null
  /** 龙虎榜卖出额(元) */
  sell_amt?: number | string | null
  /** 龙虎榜成交额(元) */
  deal_amt?: number | string | null
  turnover_pct?: number | string | null
  /** 流通市值(元) */
  free_market_cap?: number | string | null
  source?: string | null
  created_at?: string | null
}

/** 营业部席位明细行(元口径)。 */
export interface ArchiveLhbSeatItem {
  row_uid: string
  trade_date: string
  /** 'buy' | 'sell'(后端只允许这两个值, 其它值 400) */
  side: string
  symbol: string
  name?: string | null
  reason?: string | null
  operate_dept_code?: string | null
  operate_dept_name?: string | null
  close?: number | string | null
  change_pct?: number | string | null
  accum_amount?: number | string | null
  accum_volume?: number | string | null
  buy_amt?: number | string | null
  sell_amt?: number | string | null
  net_amt?: number | string | null
  /** 该席位 3 日上涨概率(%); 源未给 → null(不当 0) */
  rise_probability_3d?: number | string | null
  change_type?: string | null
  trade_id?: string | null
  total_buy_ratio?: number | string | null
  total_sell_ratio?: number | string | null
  source?: string | null
  created_at?: string | null
}

export interface ArchiveListResponse<T> {
  count: number
  /** 空榜原因(后端给); 有数据时为空串 */
  note: string
  items: T[]
}

export interface ArchiveTableFreshness {
  table: string
  rows: number | null
  rows_estimated: boolean
  earliest_date: string | null
  latest_date: string | null
}

export interface ArchiveOverviewResponse {
  tables: ArchiveTableFreshness[]
  redis_cache: { keyspace_hits: number | null; keyspace_misses: number | null; hit_rate: number | null }
  note: string
}

type QueryValue = string | number | null | undefined

/** 只拼有值的参数(空串 = 不传; 后端对缺失 date/symbol 有自己的 400 语义)。 */
function withQuery(path: string, params: Record<string, QueryValue>): string {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined) continue
    const s = String(v).trim()
    if (!s) continue
    qs.set(k, s)
  }
  const q = qs.toString()
  return q ? `${path}?${q}` : path
}

export const archiveApi = {
  /** 各落库表新鲜度(用于默认日期 = 龙虎榜表最新可用日)。 */
  overview(): Promise<ArchiveOverviewResponse> {
    return fetchAPI<ArchiveOverviewResponse>('/archive/overview', { cacheMode: 'reload' })
  },

  /**
   * 龙虎榜明细。
   * @param date   YYYYMMDD(可带 '-', 后端会归一) —— 工作区按日看榜只用 date
   * @param symbol 个股代码(与 date 至少给一个)
   * @param days   配合 symbol: 近 N 日
   */
  dragonTiger(params: { date?: string; symbol?: string; days?: number; limit?: number }): Promise<ArchiveListResponse<ArchiveDragonTigerItem>> {
    return fetchAPI<ArchiveListResponse<ArchiveDragonTigerItem>>(
      withQuery('/archive/dragon-tiger', {
        date: params.date,
        symbol: params.symbol,
        days: params.days,
        limit: params.limit,
      }),
      { cacheMode: 'reload' },
    )
  },

  /** 营业部席位明细(date+symbol 定位一只票当日榜单; side 可选 buy|sell)。 */
  lhbSeats(params: { date?: string; symbol?: string; side?: 'buy' | 'sell'; days?: number; limit?: number }): Promise<ArchiveListResponse<ArchiveLhbSeatItem>> {
    return fetchAPI<ArchiveListResponse<ArchiveLhbSeatItem>>(
      withQuery('/archive/lhb-seats', {
        date: params.date,
        symbol: params.symbol,
        side: params.side,
        days: params.days,
        limit: params.limit,
      }),
      { cacheMode: 'reload' },
    )
  },
}
