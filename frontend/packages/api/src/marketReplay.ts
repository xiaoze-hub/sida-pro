// 盘后复盘三池序列 + 连板晋级树 API 客户端(创新 1, 2026-10-24)
// 来源: src/web/api/market_replay.py
//   GET /api/market/replay/series?days=20 | from=&to=  → 近 N 交易日三池标量序列(升序)
//   GET /api/market/replay/tree?date=YYYYMMDD          → 单日连板梯队晋级树(缺省最新一期)
// 口径: 涨停/炸板/跌停/晋级率 = 东财三池同源(eastmoney_zt, 不含北交所);
//       TQ 全 A 自算(tq_pool)只作交叉校验, 各自带标签禁止混算;
//       缺数据显式 available=false / null, 绝不补 0。
import { fetchAPI } from './client'

/** 序列单项: available=false 的占位行数值字段全 null(缺数据 ≠ 0) */
export interface ReplaySeriesItem {
  trade_date: string
  trade_date_iso: string
  available: boolean
  limit_up_count: number | null
  broken_count: number | null
  limit_down_count: number | null
  break_rate_pct: number | null
  seal_rate_pct: number | null
  promo_rate_pct: number | null
  /** 晋级率样本数 = 上一交易日涨停家数(仅相邻交易日有差分时非 null) */
  promo_prev_count: number | null
  max_streak: number | null
  first_board_count: number | null
  ge2_count: number | null
  ge3_count: number | null
  one_word_count: number | null
  phase: string | null
  phase_label: string | null
  quality: string | null
  source: string | null
  caliber: string | null
  delta_limit_up: number | null
  delta_broken: number | null
  delta_break_rate_pp: number | null
  delta_limit_down: number | null
  delta_max_streak: number | null
  delta_ge2: number | null
  prev_date: string | null
}

export interface ReplaySeriesResp {
  caliber: string
  caliber_note: string
  days: number
  count: number
  items: ReplaySeriesItem[]
  note?: string | null
}

/** 梯队单层成员(payload 明细, 受采集 Top40 截断) */
export interface ReplayTreeMember {
  symbol: string | null
  name: string | null
  board_days: number
  pct?: number | null
  price?: number | null
  seal_amount_wan?: number | null
  first_seal?: string | null
  open_times?: number | null
  industry?: string | null
  circ_mv_yi?: number | null
}

/** 单个板级层级: count 是表内真实家数; members 是已落库明细(可能不全) */
export interface ReplayTreeLevel {
  board: number
  count: number
  members: ReplayTreeMember[]
  members_truncated: boolean
}

/** 晋级率 + 三件套(样本数 / 同池基准缺口 / 一字板剔除缺口) */
export interface ReplayPromotion {
  available: boolean
  promo_rate_pct: number | null
  prev_date?: string | null
  prev_count?: number | null
  promoted?: number | null
  sample_count: number | null
  sample_note: string
  baseline_available: false
  baseline_note: string
  excludes_one_word: false
  one_word_note: string
  one_word_count: number
  per_level_available: false
  per_level_note: string
}

export interface ReplayTreeResp {
  available: true
  trade_date: string
  trade_date_iso: string
  market: string
  source: string | null
  caliber: string
  quality: string | null
  missing_dims: string[]
  limit_up_count: number
  broken_count: number
  break_rate_pct: number | null
  seal_rate_pct: number | null
  limit_down_count: number
  limit_down_max_days: number
  max_streak: number
  first_board_count: number
  ge2_count: number
  ge3_count: number
  ladder_available: boolean
  levels: ReplayTreeLevel[]
  detail_returned: number
  detail_partial: boolean
  detail_note: string | null
  promotion: ReplayPromotion
  prev_comparison: { available: boolean; [k: string]: unknown }
  phase: string | null
  phase_label: string | null
  emotion_available: boolean
  emotion_basis_days?: number | null
  cross_check: {
    eastmoney_zt: number | null
    tq_pool: number | null
    tq_max_streak: number | null
    diff: number | null
    note: string | null
    tq_available: boolean
  }
  seal_mv_ratio: null
  seal_mv_ratio_note: string
  updated_at: string | null
}

/** 请求日期无快照: available=false + 最近一期提示 */
export interface ReplayTreeUnavailable {
  available: false
  note: string
  latest_date: string | null
  latest_date_iso: string
  caliber: string
}

export type ReplayTreeResult = ReplayTreeResp | ReplayTreeUnavailable

export const marketReplayApi = {
  /** 近 N 个交易日三池标量序列(升序; 缺数据交易日 available=false 占位)。 */
  series: (params?: { days?: number; from?: string; to?: string }) => {
    const q = new URLSearchParams()
    if (params?.days != null) q.set('days', String(params.days))
    if (params?.from) q.set('from', params.from)
    if (params?.to) q.set('to', params.to)
    const qs = q.toString()
    return fetchAPI<ReplaySeriesResp>(`/market/replay/series${qs ? `?${qs}` : ''}`)
  },

  /** 单日连板梯队晋级树(缺省最新一期; date 接受 YYYYMMDD / YYYY-MM-DD)。 */
  tree: (date?: string) => {
    const qs = date ? `?date=${encodeURIComponent(date)}` : ''
    return fetchAPI<ReplayTreeResult>(`/market/replay/tree${qs}`)
  },
}
