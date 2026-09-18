import type { KlineSummary } from '@panwatch/biz-ui/components/suggestion-badge'
import type { MainIntentStructured } from '@panwatch/biz-ui/lib/main-intent-types'

export interface StockInsightModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  symbol: string
  market: string
  stockName?: string
  hasPosition?: boolean
}

export interface QuoteResponse {
  symbol: string
  market: string
  name: string | null
  current_price: number | null
  change_pct: number | null
  change_amount: number | null
  prev_close: number | null
  open_price: number | null
  high_price: number | null
  low_price: number | null
  volume: number | null
  turnover: number | null
  turnover_rate?: number | null
  volume_ratio?: number | null
  pe_ratio?: number | null
  pb_ratio?: number | null
  total_market_value?: number | null
  circulating_market_value?: number | null
}

export interface MoreInfoResponse {
  symbol: string
  market: string
  turnover_rate: number | null
  volume_ratio: number | null
  commission_ratio: number | null
  total_market_value: number | null
  circulating_market_value: number | null
  change_pct: number | null
  change_pct_5d: number | null
  change_pct_20d: number | null
  change_pct_ytd: number | null
  limit_up_amount: number | null
  limit_up_ratio: number | null
  open_amount: number | null
  open_limit_buy: number | null
  consecutive_limit_days: number | null
  consecutive_up_days: number | null
  pe_dynamic: number | null
  pe_ttm: number | null
  pb: number | null
  dividend_yield: number | null
  beta: number | null
  ma5_price: number | null
  high_52w: number | null
  low_52w: number | null
  l2_tick_num: number | null
  l2_order_num: number | null
  total_buy_vol: number | null
  total_sell_vol: number | null
  cancel_buy: number | null
  cancel_sell: number | null
  zjl: number | null
  zjl_hb: number | null
  raw: Record<string, string>
  quote_time: string | null
}

export interface DarkFlowTqResponse {
  symbol?: string
  market?: string
  date?: string
  xl_net: number | null
  large_net: number | null
  mid_net: number | null
  small_net: number | null
  total_orders: number | null
  reconstructed_orders: number | null
  split_order_count: number | null
  avg_split_parts: number | null
  cancel_ratio: number | null
  cancel_buy_vol: number | null
  cancel_sell_vol: number | null
  tuopan: boolean | null
  yapan: boolean | null
  suopan: boolean | null
  data_status: string
}

export interface CompanyInfo {
  symbol?: string
  name?: string | null
  ename?: string | null
  industry?: string | null
  area?: string | null
  market_board?: string | null
  list_status?: string | null
  list_date?: string | null
  reg_capital?: string | null
  issuer?: string | null
  secretary?: string | null
  phone?: string | null
  website?: string | null
  address?: string | null
  bscope?: string | null
  desc?: string | null
  concepts?: string | null
  note?: string | null
}

/**
 * `GET /klines/{symbol}/summary` 的**顶层** `orderbook` 字段(注意: 不在 `summary` 里面)。
 *
 * 后端证据:
 *  - 装配处 `src/web/api/klines.py:518-545`(`_build_layer_data` 的 ⓪ 段)—— 优先 `.img` 离线委托队列
 *    (命中时额外带 `img_path`), 无 `.img` 时退回 thsdk 实时快照(带硬超时), 都拿不到 → `None`;
 *  - 字段口径 `src/core/orderbook_engine.py:217-271`(`order_book_queue`)。
 *
 * 诚实约束(与后端同纪律): 空快照时后端只回 `{available:false, shape:null, note:'无数据'}` ——
 * 其余字段**整体缺失**; 消费方一律"缺 → `--`", 不得用别的口径顶替或编造。
 */
export interface SummaryOrderbook {
  /** 是否有真实盘口价(空快照/源不可用 = false, 见 orderbook_engine.py:258-260 的复核注) */
  available?: boolean
  /** 快照来源(后端 `snapshot.source`, 如 thsdk / img) */
  source?: string | null
  /** 最优买价(买档最高价, 元) */
  best_bid?: number | null
  /** 最优卖价(卖档最低价, 元) */
  best_ask?: number | null
  /** 价差 = best_ask − best_bid(元, 后端 round 6 位); 任一价缺失 → null */
  spread?: number | null
  /** 买盘力量占比 = 买委托量合计 /(买 + 卖委托量合计), 0~1; 总量为 0 → null */
  bid_pressure?: number | null
  /** 委托队列总量(股); 无 `.img` 队列时缺失 */
  queue_shares?: number | null
  /** 队列失衡 = 队列总量 − 卖一量(股); 两者缺一即 null */
  queue_imbalance?: number | null
  /** 形态: `托盘`(占比 ≥0.6) / `压盘`(≤0.4) / `均衡`(其间); 无占比 → null(不猜) */
  shape?: string | null
  /** 命中 `.img` 离线文件时下发其路径(klines.py:530) */
  img_path?: string | null
  /** `available=false` 时后端给的说明原文(如「无数据」)—— 原样展示, 不本地编理由 */
  note?: string | null
}

export interface KlineSummaryResponse {
  symbol: string
  market: string
  summary: KlineSummary
  /** 2026-08-12 预热优化: 主力意图结构化(逐笔口径), 供 K线 tab 的 InteractiveKline 秒显图例卡 */
  main_intent_structured?: MainIntentStructured | null
  // ============== SIDA Pro 设计稿 v2.0: K线图层标注数据 (2026-09-01) ==============
  /** L2 GS 买卖点序列 (.tck 盘后/日线算法). 后端 P2 阶段输出 */
  gs_signals?: GsSignalLike[]
  /** L3 资金柱 (明盘+暗盘). 后端 P2 阶段输出 */
  fund_flow?: FundFlowBarLike[]
  /** L4 事件标注. 后端 P2 阶段输出 */
  events?: KlineEventLike[]
  /**
   * 盘口形态/最优买卖/价差/买盘占比(v0.6.0 遗留④ 起暴露给消费方)。
   * **顶层字段**, 与 `summary` 平级(klines.py:879-891 的 `result` 里 `**_build_layer_data(...)`);
   * 仅 A 股计算。
   *
   * 空值有**两种不同形态**(2026-09-14 复审订正; 原注"非 CN 或源不可用 → null"是错的):
   *  - **源不可用/空快照** → 不是 `null`, 而是一个**对象** `{available:false, shape:null, note:'无数据'}`
   *    (`orderbook_engine.order_book_queue` 的显式空快照分支) —— 此时应把 `note` **原样转述**, 不本地编理由;
   *  - **非 CN 标的**, 或 `klines.py:518-545` 那段装配整体抛异常 → 才是 `null`。
   * 生产实测(2026-09-14 休市, 无 `.img` 且 thsdk 不可达): `/klines/600519/summary` 的 `orderbook`
   * 返回的正是 `{"available": false, "shape": null, "note": "无数据"}` ⇒ 走的是第一种。
   */
  orderbook?: SummaryOrderbook | null
}
// 镜像 InteractiveKline 的新图层类型, 避免循环 import (组件已 export 同名 type)
export type GsSignalLike = { date: string; side: 'G' | 'S'; confirmed: boolean; price: number }
// `ming_net` 是后端真实字段(明盘); `open_net` 是早期误写的别名, 保留兼容(见 KlineChart FundFlowBar)。
export type FundFlowBarLike = {
  date: string
  ming_net?: number | null
  open_net?: number | null
  dark_net?: number | null
}
export type KlineEventLike = { date: string; kind: string; label?: string }

export interface MiniKlineResponse {
  symbol: string
  market: string
  klines: Array<{
    date: string
    open: number
    close: number
    high: number
    low: number
    volume: number
  }>
}

export interface NewsItem {
  source: string
  source_label: string
  title: string
  content?: string
  publish_time: string
  url: string
  symbols?: string[]
}

export interface HistoryRecord {
  id: number
  agent_name: string
  stock_symbol: string
  analysis_date: string
  title: string
  content: string
  suggestions?: Record<string, any> | null
  news?: Array<{
    source?: string
    title?: string
    publish_time?: string
    url?: string
  }> | null
  quality_overview?: Record<string, any> | null
  context_summary?: Record<string, any> | null
  context_payload?: Record<string, any> | null
  prompt_context?: string | null
  prompt_stats?: Record<string, any> | null
  news_debug?: Record<string, any> | null
  created_at: string
  updated_at?: string
}

export interface PortfolioPosition {
  symbol: string
  market: string
  quantity: number
  cost_price: number
  market_value_cny: number | null
  pnl: number | null
}

export interface PortfolioSummaryResponse {
  accounts: Array<{
    positions: PortfolioPosition[]
  }>
}

export type InsightTab = 'overview' | 'kline' | 'suggestions' | 'news' | 'announcements' | 'reports' | 'deep' | 'company' | 'fundamentals'

export interface StockAgentInfo {
  agent_name: string
  schedule?: string
  ai_model_id?: number | null
  notify_channel_ids?: number[]
}

export interface StockItem {
  id: number
  symbol: string
  name: string
  market: string
  agents?: StockAgentInfo[]
}

export const AGENT_LABELS: Record<string, string> = {
  daily_report: '盘后日报',
  premarket_outlook: '盘前分析',
  news_digest: '新闻速递',
}
