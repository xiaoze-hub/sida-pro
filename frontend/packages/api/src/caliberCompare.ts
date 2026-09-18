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

// ── 口径漂移(B5, 2026-09-18) ────────────────────────────────────────────────
// 后端 `GET /api/caliber-compare/{symbol}/drift?days=N`。逐日留痕来自收盘后的定时采集。
// 纪律: 每源一条序列(**不取平均、不合成单一权威数字**); 跨源比较必须写明比的是哪两个字段,
// 并标注那是**口径差异不是误差**; 没留痕的日期如实返回「该日未留痕」。

/** 某一源在某一天的留痕值(缺失就是缺失, 不补 0) */
export interface CaliberDriftPoint {
  value: number | null
  available: boolean
  /** 不可用原因(如「该日未留痕」), 照实显示 */
  reason: string
  /** 'suspect' = 源自标可疑 */
  quality: string
  unit: string
}

export interface CaliberDriftDay {
  trade_date: string
  sources: Record<string, CaliberDriftPoint>
}

export interface CaliberDriftComparison {
  left: { source: string; field: string }
  right: { source: string; field: string }
  /** 两端都 available 的天数 —— 样本量, 不插值凑数 */
  both_available_days: number
  mean_abs_diff: number | null
  max_abs_diff: number | null
  note: string
}

export interface CaliberDriftResponse {
  symbol: string
  days: number
  /** 每个源用于跨源对比的那个字段(显式声明, 不自动猜) */
  field_by_source: Record<string, string>
  series: CaliberDriftDay[]
  comparisons: CaliberDriftComparison[]
  archived_days: number
  note: string
}

export const caliberDriftApi = {
  get: (symbol: string, days = 30) =>
    fetchAPI<CaliberDriftResponse>(`/caliber-compare/${symbol}/drift?days=${days}`, {
      cacheMode: 'reload',
    }),
}
