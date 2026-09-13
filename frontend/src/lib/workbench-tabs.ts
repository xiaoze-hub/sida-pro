export type WorkbenchType = 'stock' | 'index' | 'board'
export type WorkbenchTab = 'l2' | 'suggest' | 'fundamental' | 'news' | 'research' | 'forecast'

export const WORKBENCH_TABS: { id: WorkbenchTab; label: string }[] = [
  { id: 'l2', label: '盘口资金' },
  { id: 'suggest', label: '建议' },
  { id: 'fundamental', label: '基本面' },
  { id: 'news', label: '消息' },
  { id: 'research', label: '研究' },
  { id: 'forecast', label: '预测' },
]

export function normalizeType(v: string | null | undefined): WorkbenchType {
  return v === 'index' || v === 'board' ? v : 'stock'
}

export function parseTab(v: string | null | undefined): WorkbenchTab {
  const hit = WORKBENCH_TABS.find((t) => t.id === v)
  return hit ? hit.id : 'l2'
}

/** 个股专属块(带1 建议条/右栏个股卡/6 标签)仅在 type=stock 显示 */
export function showStockOnly(t: WorkbenchType): boolean {
  return t === 'stock'
}

/** 去重归属表: 数据点 → 唯一归属(单测断言防漂移; 对应 spec §三) */
export const DATA_OWNERSHIP: Record<string, string> = {
  decision_indicators: 'rail.decision',
  resonance_verdict: 'rail.decision',
  kline_main: 'band2.chart',
  orderbook_ladder: 'tab.l2',
  main_net_inflow: 'tab.l2',
  pe_pb_dividend: 'band1.snapshot',
  blocks: 'rail.blocks',
  seal_amount: 'band1.snapshot',
  // 遗留⑤ 新增的带1 快照格(与 seal_amount 同归属); 不登记就等于不受去重契约保护(复审 Minor 7)
  volume: 'band1.snapshot',
  amplitude: 'band1.snapshot',
  seal_quality: 'tab.l2',
  // 遗留④ 起取自 `/klines/{s}/summary` 顶层 `orderbook` 的三个盘口读数(与 orderbook_ladder 同归属)。
  // 注: 右栏「盘口速览」的五档买一/卖一与本表的 `best_bid_ask` 是**同源不同口径**的两个面
  // (通达信 /l2 实时 vs thsdk summary 5 分钟缓存), spec 去重表 #3 明确允许并存, 故不算重复拥有。
  orderbook_shape: 'tab.l2',
  best_bid_ask: 'tab.l2',
  bid_spread: 'tab.l2',
  limit_price_boards: 'band1.snapshot',
  fund_flow: 'tab.l2',
  dark_flow_tq: 'tab.l2',
  technical_suggestion: 'band1.suggestStrip',
  intraday_monitor: 'tab.suggest',
  announcements: 'tab.news',
  news: 'tab.news',
  reports: 'tab.research',
  deep: 'tab.research',
  company_intro: 'tab.fundamental',
  lhb_margin_holders: 'tab.fundamental',
  forecast_models: 'tab.forecast',
}
