import type { KlineSummary } from '@panwatch/biz-ui/components/suggestion-badge'
import type { MainIntentStructured } from '@panwatch/biz-ui/components/InteractiveKline'

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
}
// 镜像 InteractiveKline 的新图层类型, 避免循环 import (组件已 export 同名 type)
export type GsSignalLike = { date: string; side: 'G' | 'S'; confirmed: boolean; price: number }
export type FundFlowBarLike = { date: string; open_net?: number | null; dark_net?: number | null }
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
