/**
 * 决策账本 API 客户端(B6 前端 / 设计稿 v3.0 §八「新增能力」)。
 *
 * 后端契约(2026-09-18, v175 `decision_log`):
 *  · `GET /api/decisions/stats?days&min_sample` —— 按信号类型统计 T+1/3/5 命中率。
 *    **样本不足时 `hit_rate` 为 null 且 `insufficient=true`** —— 页面必须显示"样本不足",
 *    不许把 null 当 0 或自己算百分比(诚实口径)。
 *  · `GET /api/decisions/log?kind&limit` —— 信号明细(当时价格 + 证据快照 + 回填状态)。
 *    `ret/hit` 为 null 表示"还没回填"(需要未来的 K 线才算得出来), 不是 0。
 */
import { fetchAPI } from './client'

/** 单个时间档(T+1 / T+3 / T+5)的统计。 */
export interface DecisionHorizon {
  /** 已回填样本数(**只算有真实结果的行**, 未回填不进分母) */
  n: number
  /** 命中率; 样本不足或尚无样本时为 null */
  hit_rate: number | null
  /** true = 不给数字(页面显示"样本不足") */
  insufficient: boolean
  /** 为什么不给(空串=可以给) */
  note: string
}

export interface DecisionStatsRow {
  signal_kind: string
  n_total: number
  horizons: Record<'t1' | 't3' | 't5', DecisionHorizon>
}

export interface DecisionStatsResponse {
  since: string
  min_sample: number
  rows: DecisionStatsRow[]
  note: string
}

/** 一条信号明细。 */
export interface DecisionLogItem {
  signal_kind: string
  symbol: string
  trade_date: string
  /** 信号产生时的价格(元); **null = 当时没取到价, 不是 0** */
  price_at_signal: number | null
  /** 当时证据快照(JSON 字符串, 便于回放"凭什么出这个信号") */
  context: string
  source: string
  outcomes: Record<'t1' | 't3' | 't5', { ret: number | null; hit: boolean | null }>
  /** 回填时间; null = 尚未回填 */
  filled_at: string | null
}

export interface DecisionLogResponse {
  count: number
  /** 过滤后总行数(供翻页; 旧后端无此字段时回退 count) */
  total?: number
  offset?: number
  limit?: number
  has_more?: boolean
  items: DecisionLogItem[]
  note: string
}

/** 回测单个共振态聚合(可实现口径 close_to_close)。 */
export interface BacktestPhaseAgg {
  count: number
  win_rate: number | null
  profit_ratio: number | null
  avg_gain: number | null
  avg_loss: number | null
  basis?: string
  /** 路径极值(仅统计参考, 不可交易) */
  path_max_gain_avg: number | null
  path_max_loss_avg: number | null
}

export interface DecisionBacktestResponse {
  /** false = 未跑出结果(缺股票池/计算失败), 页面显式标注, 不编造 */
  available: boolean
  error?: string
  params: Record<string, unknown>
  /** 口径标记: 双指标(缺资金维) / 三指标(资金=OHLC对照项) —— 必须透传到 UI */
  basis: string
  sample: { symbols: number; signals: number }
  by_phase?: Record<string, BacktestPhaseAgg>
  /** 官方 2024.10-2025.10 基准(仅对照, 不同口径不可直接比优劣) */
  official?: Record<string, { win_rate: number; pl_ratio: number }>
  generated_at?: string
  note: string
}

/** 入场候选后验一行(horizon×来源)。 */
export interface EntryOutcomeRow {
  horizon_days: number
  source: string
  source_label: string
  total: number
  wins: number
  /** 样本不足(min_sample)时为 null —— 不给小样本算出的百分比 */
  win_rate: number | null
  avg_return_pct: number | null
  insufficient: boolean
  min_sample: number
}

export interface EntryOutcomesResponse {
  window_days: number
  min_sample: number
  available: boolean
  error?: string
  rows: EntryOutcomeRow[]
  total_samples?: number
  note: string
}

/** 信号对账(批次D)单个时间档。 */
export interface SignalHorizonStat {
  n: number
  win_rate: number | null
  avg_pct: number | null
}

export interface SignalHitRateResponse {
  days: number
  by_type: Record<
    string,
    {
      t1: SignalHorizonStat
      t5: SignalHorizonStat
      /** 官方基准对照(resonance; 样本足够时才由后端附带) */
      official_benchmark?: { win_rate: number; pl_ratio: number; note: string }
    }
  >
}

/** 命中率统计(样本不足时不返回数字, 由调用方如实展示)。 */
export function fetchDecisionStats(days = 180, minSample = 30) {
  return fetchAPI<DecisionStatsResponse>(
    `/decisions/stats?days=${days}&min_sample=${minSample}`,
    { cacheMode: 'reload' },
  )
}

/** 信号明细(kind 省略 = 全部类型); 支持 offset/日期过滤分页。 */
export function fetchDecisionLog(
  kind?: string,
  limit = 100,
  offset = 0,
  startDate?: string,
  endDate?: string,
) {
  const q = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  if (kind) q.set('kind', kind)
  if (startDate) q.set('start_date', startDate)
  if (endDate) q.set('end_date', endDate)
  return fetchAPI<DecisionLogResponse>(`/decisions/log?${q.toString()}`, { cacheMode: 'reload' })
}

/**
 * 三指标共振回测(P2-1)。缺股票池/计算失败后端显式 `available=false`(永不 500), 页面须如实标注。
 * 默认 `fundSource` 空 = 双指标(缺资金维) —— 与官方四态不可直接比较, `basis` 标记必须上屏。
 */
export function fetchDecisionBacktest(params: {
  symbols: string
  startDate?: string
  endDate?: string
  holdDays?: number
  successPct?: number
  fundSource?: string
  activityLine?: number
  maxSymbols?: number
}) {
  const q = new URLSearchParams({ symbols: params.symbols })
  if (params.startDate) q.set('start_date', params.startDate)
  if (params.endDate) q.set('end_date', params.endDate)
  if (params.holdDays != null) q.set('hold_days', String(params.holdDays))
  if (params.successPct != null) q.set('success_pct', String(params.successPct))
  if (params.fundSource) q.set('fund_source', params.fundSource)
  if (params.activityLine != null) q.set('activity_line', String(params.activityLine))
  if (params.maxSymbols != null) q.set('max_symbols', String(params.maxSymbols))
  return fetchAPI<DecisionBacktestResponse>(`/decisions/backtest?${q.toString()}`, { cacheMode: 'reload' })
}

/** 入场候选后验(P2-3): 按 horizon×来源给胜率; 样本不足 `insufficient=true`(不给数字)。 */
export function fetchEntryOutcomes(days = 30, minSample = 20) {
  const q = new URLSearchParams({ days: String(days), min_sample: String(minSample) })
  return fetchAPI<EntryOutcomesResponse>(`/decisions/entry-outcomes?${q.toString()}`, { cacheMode: 'reload' })
}

/** 信号对账(批次D): 按信号类型 T+1/T+5 胜率; resonance 附官方基准对照。 */
export function fetchSignalHitRate(signalType?: string, days = 30) {
  const q = new URLSearchParams({ days: String(days) })
  if (signalType) q.set('signal_type', signalType)
  return fetchAPI<SignalHitRateResponse>(`/signals/hit-rate?${q.toString()}`, { cacheMode: 'reload' })
}

/** 信号类型的中文名(后端用机器名, 页面给人看)。 */

export const SIGNAL_KIND_LABEL: Record<string, string> = {
  resonance3: '三指标共振',
  gs_buy: 'GS 买点',
  gs_sell: 'GS 卖点',
  auction_pool: '竞价池',
}

export function kindLabel(kind: string): string {
  return SIGNAL_KIND_LABEL[kind] ?? kind
}

/** 对象式导出(与本包既有风格一致: `xxxApi.get(...)`) */
export const decisionsApi = {
  stats: (days = 180, minSample = 30) => fetchDecisionStats(days, minSample),
  log: (kind?: string, limit = 100, offset = 0, startDate?: string, endDate?: string) =>
    fetchDecisionLog(kind, limit, offset, startDate, endDate),
  backtest: (params: Parameters<typeof fetchDecisionBacktest>[0]) => fetchDecisionBacktest(params),
  entryOutcomes: (days = 30, minSample = 20) => fetchEntryOutcomes(days, minSample),
}

/** 信号对账(批次D)对象式导出。 */
export const signalsReviewApi = {
  hitRate: (signalType?: string, days = 30) => fetchSignalHitRate(signalType, days),
}
