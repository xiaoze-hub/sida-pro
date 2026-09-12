import { fetchAPI } from './client'

/** 后端由"求值器真正读的阈值键"派生出的可编辑参数(前端不必逐策略写控件)。 */
export interface StrategyParam {
  key: string
  label: string
  value: number
  unit?: string
  min: number
  max: number
  step: number
  help?: string
}

export interface StrategyItem {
  id: string
  display_name: string
  description: string
  category: string
  tags: string[]
  ui_badge: string
  source: string
  filter: Record<string, number | string>
  params?: StrategyParam[]
  eod_fields: string[]
  data_window: 'realtime' | 'eod'
  available_now: boolean
}

export interface StrategyListResponse {
  items: StrategyItem[]
  total: number
}

export interface ApplyRequest {
  strategy_id: string
  symbol: string
  market?: string
  /** 只认该策略 params 声明过的阈值键; 未声明的键后端会静默丢弃 */
  overrides?: Record<string, number>
}

export interface ScoreFactor {
  factor: string
  raw: number | string
  score: number
  weight: number
}

export interface ApplyResult {
  strategy_id: string
  symbol: string
  market: string
  passed: boolean
  score: number
  score_breakdown: ScoreFactor[]
  failed_filters: { field: string; actual: number; required: string; threshold: number }[]
  missing_fields: string[]
  current_data: Record<string, number | string | null>
  error?: string
}

export interface ScanRequest {
  strategy_id: string
  market?: string
  limit?: number
  universe?: 'all' | 'watchlist'
  min_score?: number
  symbol_limit?: number
  /** 自定义股票池(共振查询精筛): 传入则只扫这几只, 优先于 universe, ≤100 只 */
  symbols?: string[]
  /** 参数覆盖(与 apply 同规则): 只认该策略 params 声明过的阈值键 */
  overrides?: Record<string, number>
}

export interface ScanItem {
  symbol: string
  name: string
  market: string
  score: number
  score_breakdown: ScoreFactor[]
  current_data: Record<string, number | string | null>
  missing_fields: string[]
}

export interface ScanResult {
  items: ScanItem[]
  total: number
  scanned: number
  quoted: number
  message?: string
}

export const strategiesApi = {
  list: () => fetchAPI<StrategyListResponse>(`/strategies/list`),

  get: (id: string) => fetchAPI<StrategyItem>(`/strategies/${encodeURIComponent(id)}`),

  apply: (req: ApplyRequest) =>
    fetchAPI<ApplyResult>(`/strategies/apply`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  scan: (req: ScanRequest) =>
    fetchAPI<ScanResult>(`/strategies/scan`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),
}