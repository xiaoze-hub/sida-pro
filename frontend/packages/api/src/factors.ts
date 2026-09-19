import { fetchAPI } from './client'

export interface FactorWeight {
  factor_code: string
  market: string
  weight: number
  is_pinned: boolean
  auto_calibrate: boolean
  last_ic: number | null
  last_ir: number | null
  last_sample_size: number | null
  last_calibrated_at: string | null
  reason: string
  updated_at: string | null
}

export interface FactorWeightUpdatePayload {
  weight?: number
  is_pinned?: boolean
  auto_calibrate?: boolean
}

export const factorsApi = {
  /** 因子权重列表(每因子按市场区分,含最近 IC/IR 标定结果)。 */
  list: () => fetchAPI<{ items: FactorWeight[] }>('/factors/weights'),

  /** 更新单个因子权重(手动权重 / 锁定 / 自动标定开关)。 */
  update: (factorCode: string, market: string, patch: FactorWeightUpdatePayload) =>
    fetchAPI<FactorWeight>(`/factors/weights/${factorCode}/${market}`, {
      method: 'POST',
      body: JSON.stringify(patch),
    }),
}

// ── 因子有效性(B9 数据资产 E1, 2026-09-19) ────────────────────────────────
// 后端契约: `GET /recommendations/strategy-factor-ic?days&horizon`
// 口径(见 src/core/factor_eval.py 文档头): **横截面 Spearman IC 为主口径**,
// `ic_pooled` 只是参考值(混入时序变异, 不作决策口径);`ic` 需 **≥3 个期数**才算,
// 不足时为 null —— 页面必须显示 `--` 并说明原因, **不许自己算 IC 或把 null 当 0**。

export interface FactorIC {
  /** 主口径: 横截面 IC 均值; 期数 <3 时为 null(不是 0) */
  ic: number | null
  /** IC 的 t 统计量; 样本不足或方差为 0 时为 null */
  ic_t: number | null
  ic_std: number | null
  /** **参考值**(pooled, 混时序变异) —— 不作为决策口径 */
  ic_pooled: number | null
  /** 样本外段 IC(后 30% 交易日); 期数 <2 时为 null */
  ic_holdout: number | null
  ir: number | null
  sample_size: number
  /** 参与 IC 计算的期数(截面交易日数) */
  ic_periods: number
  holdout_periods: number
}

export interface FactorICResp {
  horizon: number
  days: number
  market: string
  holdout_ratio: number
  /** factor_code → 指标; 出错时为空对象且带 error */
  factors: Record<string, FactorIC>
  /** 后端计算失败时原样带出(页面照实显示, 不装作"没数据") */
  error?: string
}

export const factorICApi = {
  evaluate: (days = 90, horizon = 5) =>
    fetchAPI<FactorICResp>(`/recommendations/strategy-factor-ic?days=${days}&horizon=${horizon}`),
}
