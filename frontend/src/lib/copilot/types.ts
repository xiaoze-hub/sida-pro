/**
 * AI 数智参谋（Copilot）前端类型 —— 与后端 Pydantic 一一对应。
 *
 * 契约源：`docs/research/AI数智参谋全链路落地设计方案.md` §4.3 / §4.4 / §4.5。
 * 铁律：**不在这里造字段**。后端没给的字段一律 `null`（类型里已经写 `| null`），
 * 前端不许用 `?? 0` 把它补成数字（R7 门禁 + 「缺数据不装成 0」的产品红线）。
 */

export type Tone = 'opportunity' | 'neutral' | 'caution' | 'risk'

export type DefenseSlot = 'first' | 'core' | 'probe' | 'cost' | 'cap'

export interface EvidenceBase {
  available: boolean
  /** 口径串，卡片脚注原样显示（例如「自算CYQ·120日」） */
  caliber: string
  as_of: string | null
  /** 缺失原因原文，来自后端；前端不改写、不猜 */
  degraded_reason: string | null
  /** true = 权限不足（不是没数据），显示「升级解锁」而不是 `--` */
  gated: boolean
}

export interface ChipEvidence extends EvidenceBase {
  main_cost: number | null
  peak_price: number | null
  peak_ratio: number | null
  cost_70_low: number | null
  cost_70_high: number | null
  cost_90_low: number | null
  cost_90_high: number | null
  /** 0-100，后端已 ×100 */
  profit_ratio: number | null
  profit_state: string | null
  concentration: number | null
  pattern: string | null
  status_label: string | null
  signal_level: 'bullish' | 'neutral' | 'warning' | null
  departure_pct: number | null
  lookback_days: number | null
}

export interface ResonanceEvidence extends EvidenceBase {
  gs_zone: string | null
  gs_signal: 'G' | 'S' | null
  gs_signal_date: string | null
  gs_pending: boolean | null
  activity: number | null
  activity_level: string | null
  activity_above_strong: boolean | null
  activity_streak_days: number | null
  activity_ma5: number | null
  /** L2 主力净额（万元，TQ 原始单位） */
  l2_net_wan: number | null
  l2_direction: string | null
  l2_cancel_rate: number | null
  l2_cancel_bias: number | null
  resonance_state: string | null
  resonance_action: string | null
  // 'unknown' = 证据不足(A-01 三态化): 资金维断链时只能是它, 不许下方向结论
  resonance_warning: 'opportunity' | 'safe' | 'caution' | 'warning' | 'unknown' | null
}

export interface MicroEvidence extends EvidenceBase {
  /** 0~1，展示层 ×100，且只乘一次 */
  b_cancel_ratio: number | null
  s_cancel_ratio: number | null
  cancel_bias: number | null
  activity_score: number | null
  intent_tag: string | null
  intent_desc: string | null
  warning_level: 'safe' | 'caution' | 'warning' | null
  volume_ratio: number | null
  turnover_rate: number | null
  /** true = 盘中会话值为 0，已回退收盘快照 */
  session_fallback: boolean
}

export interface WyckoffMarker {
  code: string
  name: string
  date: string | null
  direction: string | null
  basis: string[]
  definition?: string | null
}

export interface WyckoffEvidence extends EvidenceBase {
  stage: string | null
  stage_label: string | null
  effort_result: 'divergence' | 'match' | 'unknown' | null
  markers_count: number | null
  recent: WyckoffMarker[]
  note: string | null
}

export interface AuctionEvidence extends EvidenceBase {
  verdict_tag: string | null
  verdict_level: 'strong_buy' | 'buy' | 'warning' | 'risk' | 'neutral' | null
  verdict_action: string | null
  gap_pct: number | null
  /** 竞价成交额（元） */
  open_amount: number | null
  volume_ratio: number | null
  withdraw_rate: number | null
  open_zt_buy: number | null
}

export interface DemonEvidence extends EvidenceBase {
  total: number | null
  grade: string | null
  max_streak: number | null
  n_events: number | null
  n_sealed: number | null
  participation: number | null
  dims: Record<string, number>
  flags: string[]
  in_top50: boolean | null
  rank: number | null
}

export interface ThemeEvidence extends EvidenceBase {
  group: string | null
  rank: number | null
  limit_up_count: number | null
  leader: string | null
  is_top5: boolean | null
}

export interface EvidenceBundle {
  chips: ChipEvidence
  resonance: ResonanceEvidence
  microstructure: MicroEvidence
  wyckoff: WyckoffEvidence
  auction: AuctionEvidence
  demon: DemonEvidence
  theme: ThemeEvidence
}

export interface DefenseLine {
  slot: DefenseSlot
  label: string
  price: number
  basis: string
  source: 'intraday_low' | 'main_cost' | 'cost_70_low' | 'cost_70_high' | 'spring_low'
  side: 'support' | 'resistance'
  distance_pct: number
}

export interface PlanTrigger {
  price: number | null
  kind: 'defend' | 'reduce' | 'entry' | 'invalidate'
  note: string
}

export interface ActionPlan {
  audience: 'holder' | 'empty'
  action: 'reduce' | 'hold' | 'defend' | 'wait' | 'probe' | 'avoid'
  title: string
  text: string
  triggers: PlanTrigger[]
}

export interface CopilotVerdict {
  intent_label: string
  intent_tone: Tone
  /** 0~1 */
  confidence: number
  evidence_used: string[]
  evidence_missing: string[]
  narrative: string
  defense_lines: DefenseLine[]
  action_plans: ActionPlan[]
  risk_flags: string[]
  caliber_notes: string[]
  disclaimer: string
  rule_id: string
}

export interface CopilotSession {
  phase: 'pre_open' | 'auction' | 'intraday' | 'closed' | 'non_trading'
  is_trading: boolean
  stale: boolean
  age_s: number | null
  trade_date: string | null
}

export interface CopilotQuote {
  available: boolean
  close: number | null
  change_pct: number | null
  prev_close: number | null
  open: number | null
  high: number | null
  low: number | null
  source: string | null
  as_of: string | null
  degraded_reason: string | null
}

export interface CopilotBrief {
  symbol: string
  market: string
  name: string | null
  as_of: string
  trade_date: string | null
  session: CopilotSession
  quote: CopilotQuote
  evidence: EvidenceBundle
  verdict: CopilotVerdict
  cache: { hit: boolean; ttl_s: number; key: string }
  version: string
}

export interface CopilotPreset {
  id: string
  icon: string
  label: string
  prompt: string
  group: 'auction' | 'chip' | 'resonance' | 'demon' | 'defense'
  requires_perm: string | null
}

/** 证据块键名（顺序 = 卡片证据条顺序） */
export const EVIDENCE_KEYS = [
  'chips',
  'resonance',
  'microstructure',
  'wyckoff',
  'auction',
  'demon',
  'theme',
] as const

export type EvidenceKey = (typeof EVIDENCE_KEYS)[number]
