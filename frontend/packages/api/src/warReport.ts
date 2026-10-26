// 主力资金战报 API 客户端(规格 §4.4, 2026-10-10)
// 来源: src/web/api/war_report.py
//   GET  /api/war-report/daily    → WarReportResp(无快照 → available:false + note)
//   POST /api/war-report/refresh  → 手动触发构建 + 落库(owner)
import { fetchAPI } from './client'

/** 战报个股行(全市场大单净流入, thsdk DDE 口径) */
export interface WarReportRow {
  symbol: string
  name?: string | null
  /** 主力净流入(万元) */
  main_net_wan: number
  /** 个股主力净额变化(采样区间增量, 万元); 无采样 → null(显式缺失) */
  net_change_wan?: number | null
  /** 当日采样样本数 */
  samples?: number
  last_sample_ts?: string | null
  source?: string
  caliber?: string
}

export interface WarReportIndustryRow {
  name: string
  code: string
  /** 板块主力资金(亿元, TQ SUPAMO) */
  fund_net_yi: number
}

/** 缺源即显式 available=false + note 的子块(行业分布 / 拆单对倒计数) */
export interface WarReportBlock {
  available: boolean
  note?: string
  rows?: unknown[]
  top?: WarReportIndustryRow[]
  bottom?: WarReportIndustryRow[]
  split_count?: number | null
  wash_count?: number | null
  covered?: string[]
}

export interface WarReportSnapshot {
  available: true
  snapshot_date: string
  market: string
  updated_at: string | null
  generated_at?: string | null
  caliber?: string
  universe?: number
  computed?: number
  top_inflow: WarReportRow[]
  top_outflow: WarReportRow[]
  industry: WarReportBlock
  split_wash: WarReportBlock
  note?: string
}

export interface WarReportUnavailable {
  available: false
  note: string
}

export type WarReportResp = WarReportSnapshot | WarReportUnavailable

export const warReportApi = {
  /** 读最新主力资金战报快照(无快照 → available:false)。 */
  daily: (params?: { market?: string }) => {
    const q = params?.market ? `?market=${encodeURIComponent(params.market)}` : ''
    return fetchAPI<WarReportResp>(`/war-report/daily${q}`)
  },

  /** 手动触发战报构建 + 落库(同步, 全市场约 16s; 需登录 owner)。 */
  refresh: () => fetchAPI<WarReportResp>('/war-report/refresh', { method: 'POST' }),
}
