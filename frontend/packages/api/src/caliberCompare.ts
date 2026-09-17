import { fetchAPI } from './client'

/**
 * 口径对照(2026-09-18) —— 明盘 L2 / 暗盘逐笔 / 东财四档 三口径并排。
 *
 * 后端 `GET /api/caliber-compare/{symbol}`(pro 档: view_forecast)。
 * 强调: 这是**消歧**工具 —— 三源数字不同是口径不同, 不合成单一"权威数字"。
 */

export interface CaliberField {
  label: string
  /** 元/笔/%(由 source.unit 或字段自带 unit 决定), 缺失为 null —— 前端显示 `--`, 不补 0 */
  value: number | null
  unit?: string
}

export interface CaliberSource {
  key: 'thsdk_l2' | 'tencent_dark' | 'eastmoney_flow' | string
  name: string
  caliber: string
  unit: string
  available: boolean
  fields: CaliberField[]
  /** 不可用原因 / 数据可疑提示 / 基准日说明(照实显示) */
  note: string
  date?: string | null
}

export interface CaliberDifference {
  topic: string
  detail: string
}

export interface CaliberCompareResponse {
  symbol: string
  market: string
  as_of: string
  sources: CaliberSource[]
  available_count: number
  differences: CaliberDifference[]
}

export const caliberCompareApi = {
  get: (symbol: string) =>
    fetchAPI<CaliberCompareResponse>(`/caliber-compare/${symbol}`, { cacheMode: 'reload' }),
}
