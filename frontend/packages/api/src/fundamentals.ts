import { fetchAPI } from './client'

/**
 * 基本面明细 API —— 龙虎榜 / 融资融券 / 股东户数 / 分红 / 事件日历
 *
 * 后端端点: GET /api/market-data/fundamentals-detail/{symbol}?market=CN&dt_days=10
 * （挂在 market_data router 下, prefix=/api/market-data, 需要登录 token）
 *
 * 响应结构(与 src/web/api/market_data.py fetch_fundamentals_detail 对齐):
 *   { symbol, market, dragon_tiger[], margin[], shareholders[], dividend[], events[] }
 * 每类独立容错: 单类失败只返回空数组, 不拖垮整体; 各字段可为 null/缺省。
 *
 * 注意: 该端点由后端另一任务开发, 若尚未就绪(404/超时), 前端调用失败必须静默降级,
 * 不得阻断个股详情弹窗的其他功能。渲染层需对所有字段做空值容错。
 *
 * 字段约定: snake_case; 金额单位=元, 户数单位=户, 比例字段已为百分数值。
 */

/** 龙虎榜条目 */
export interface DragonTigerItem {
  /** 上榜日期, 如 20260812 */
  trade_date?: string | null
  symbol?: string | null
  name?: string | null
  /** 上榜原因, 如"日涨幅偏离值达7%" */
  reason?: string | null
  /** 收盘价(元) */
  close?: number | null
  /** 涨跌幅(%) */
  change_pct?: number | null
  /** 净买入额(元), 正数=净买入, 负数=净卖出 */
  net_buy?: number | null
  /** 买入额(元) */
  buy_amt?: number | null
  /** 卖出额(元) */
  sell_amt?: number | null
  /** 换手率(%) */
  turnover_pct?: number | null
}

/** 融资融券条目 */
export interface MarginItem {
  /** 数据日期 */
  date?: string | null
  symbol?: string | null
  /** 融资余额(元) */
  rz_balance?: number | null
  /** 融资买入额(元) */
  rz_buy?: number | null
  /** 融资偿还额(元) */
  rz_repay?: number | null
  /** 融券余额(元) */
  rq_balance?: number | null
  /** 融券卖出量(股) */
  rq_sell_vol?: number | null
  /** 融券偿还量(股) */
  rq_repay_vol?: number | null
  /** 两融合计余额(元) */
  total_balance?: number | null
}

/** 股东户数条目 */
export interface ShareholderItem {
  /** 报告期日期 */
  report_date?: string | null
  symbol?: string | null
  /** 股东户数(户) */
  holder_num?: number | null
  /** 较上期变动(户), 负数=户数减少(筹码集中) */
  change_num?: number | null
  /** 较上期变动比例(%, 已为百分数值) */
  change_ratio?: number | null
  /** 户均持股(股) */
  avg_shares?: number | null
}

/** 分红条目 */
export interface DividendItem {
  /** 除权除息日 */
  ex_date?: string | null
  symbol?: string | null
  /** 每股派息(元) */
  dividend_per_share?: number | null
  /** 每10股转增股数 */
  transfer_ratio?: number | null
  /** 每10股送股股数 */
  bonus_ratio?: number | null
  /** 分红进度, 如"预案/实施" */
  progress?: string | null
}

/** 事件日历条目 */
export interface EventItem {
  source?: string | null
  external_id?: string | null
  /** 事件类型, 如"业绩预告 / 股东大会 / 解禁" */
  event_type?: string | null
  /** 事件标题 */
  title?: string | null
  /** 发布时间(ISO 时间戳) */
  publish_time?: string | null
  /** 重要度 */
  importance?: number | null
  url?: string | null
}

/** 基本面明细聚合响应 */
export interface FundamentalsDetail {
  symbol?: string
  market?: string
  /** 龙虎榜(近若干期) */
  dragon_tiger?: DragonTigerItem[] | null
  /** 融资融券 */
  margin?: MarginItem[] | null
  /** 股东户数(近若干期) */
  shareholders?: ShareholderItem[] | null
  /** 分红历史(按除权日倒序) */
  dividend?: DividendItem[] | null
  /** 事件日历(近7日) */
  events?: EventItem[] | null
}

export const fundamentalsApi = {
  /** 拉取个股基本面明细(龙虎榜/两融/股东户数/分红/事件日历) */
  detail: (symbol: string, market: string, dtDays = 10) =>
    fetchAPI<FundamentalsDetail>(
      `/market-data/fundamentals-detail/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}&dt_days=${dtDays}`
    ),
}
