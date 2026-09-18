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
  items: DecisionLogItem[]
  note: string
}

/** 命中率统计(样本不足时不返回数字, 由调用方如实展示)。 */
export function fetchDecisionStats(days = 180, minSample = 30) {
  return fetchAPI<DecisionStatsResponse>(
    `/decisions/stats?days=${days}&min_sample=${minSample}`,
    { cacheMode: 'reload' },
  )
}

/** 信号明细(kind 省略 = 全部类型)。 */
export function fetchDecisionLog(kind?: string, limit = 100) {
  const q = new URLSearchParams({ limit: String(limit) })
  if (kind) q.set('kind', kind)
  return fetchAPI<DecisionLogResponse>(`/decisions/log?${q.toString()}`, { cacheMode: 'reload' })
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
  log: (kind?: string, limit = 100) => fetchDecisionLog(kind, limit),
}
