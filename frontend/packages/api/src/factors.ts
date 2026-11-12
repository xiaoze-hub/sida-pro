import { fetchAPI } from './client'

export interface FactorWeight {
  factor_code: string
  market: string
  weight: number
  is_pinned: boolean
  /** 仅决定只读标定是否跳过该因子; 不表示会自动改权重 */
  auto_calibrate: boolean
  last_ic: number | null
  last_ir: number | null
  last_sample_size: number | null
  last_calibrated_at: string | null
  /** 历史观测时间(meta 记录), 与 last_ic 同一份数据; 不是实时值 */
  last_observation_at: string | null
  /** true = 观测已过期(>7 天)或从未记录 —— UI 必须标明, 不能当当前值 */
  last_observation_stale: boolean
  observation_note: string
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

  /** 更新单个因子权重(手动权重 / 锁定 / 标定建议开关)。不会自动调权; 需 manage_settings, 否则 403。 */
  update: (factorCode: string, market: string, patch: FactorWeightUpdatePayload) =>
    fetchAPI<FactorWeight>(`/factors/weights/${factorCode}/${market}`, {
      method: 'POST',
      body: JSON.stringify(patch),
    }),

  /**
   * 实时计算一轮**只读**标定建议。零写入: `changed` 恒 0、`write_back` 恒 false
   * (界面要显示这两个真实字段, 不要写死)。
   * 需 `manage_settings`(受限管理员): 每轮都要扫快照, 不对普通成员开放。
   * 结果按市场+参数在服务端缓存 60s; computed_at 是实际计算时间, cached 标明是否命中缓存。
   * 并发保护只有**进程内** single-flight; `concurrency.scope` 恒为 process,
   * 多 worker 部署下每个 worker 可能各有 1 轮(不是全局互斥)。
   */
  calibrate: (market = 'CN', days = 90, horizon = 5, minSamples = 20) =>
    fetchAPI<FactorCalibrationResp>(
      `/factors/calibration?market=${market}&days=${days}&horizon=${horizon}&min_samples=${minSamples}`,
    ),
}

export interface FactorCalibrationProposal {
  factor_code: string
  market: string
  current_weight: number
  base_weight: number
  proposed_weight: number | null
  target: number | null
  /** proposed / unchanged / skipped_no_data / skipped_low_sample / skipped_ic_unavailable / skipped_oos_* / skipped_pinned / skipped_disabled */
  status: string
  ic: number | null
  ir: number | null
  ic_holdout: number | null
  sample_size: number
}

export interface FactorCalibrationResp {
  market: string
  mode: 'readonly'
  /** false = 计算失败, error 里有原因(不是「没有建议」) */
  ok: boolean
  error?: string
  /** 本次实际计算时间(缓存命中时仍为首次计算时间) */
  computed_at: string
  cached: boolean
  cache_age_seconds: number
  cache_ttl_seconds: number
  checked: number
  proposed: number
  /** 本次真正改了多少条生效权重; 只读模式恒为 0 —— UI 必须显示这个字段, 不要硬编码 */
  changed: number
  /** true 才会写回生效权重; 只读模式恒为 false */
  write_back: boolean
  /**
   * 这次计算实际受到的并发保护范围。缓存命中时描述的是产生这份结果的那一轮。
   * 后端已移除跨 worker 租约: `scope` 恒为 process、`cross_worker_exclusive`
   * 恒为 false —— 只有本进程 single-flight, 多 worker 下每个 worker 可能各有 1 轮。
   * `cache_scope` 恒为 process(缓存不跨 worker 共享)。
   */
  concurrency?: {
    scope: 'cluster' | 'process'
    cross_worker_exclusive: boolean
    cache_scope: 'process'
    note: string
  }
  /** 失败时的关联号(与后端日志 [err:<ref>] 对应); error 文案里已含, 便于报障 */
  error_ref?: string
  /** true = 本轮超出墙钟预算被中止(未写入任何数据) */
  timeout?: boolean
  /** 库里已有多少权重行; 0 说明这是「无行」而不是「没样本」 */
  rows_present: number
  no_rows: boolean
  skipped_oos: number
  skipped_low_sample: number
  skipped_no_data: number
  skipped_ic_unavailable: number
  skipped_pinned: number
  skipped_disabled: number
  proposals: FactorCalibrationProposal[]
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


// ── 因子 IC 时序(B9 收口, 2026-09-19): 每日快照, 看趋势/失效拐点 ──────────────
// 诚实口径: **样本不足的那天也有行**, 只是 `ic` 为 null —— 前端要把 null 与 0 分开显示。
export interface FactorICPoint {
  factor_code: string
  trade_date: string
  ic: number | null
  ic_holdout: number | null
  ir: number | null
  sample_size: number | null
  ic_periods: number | null
}

export interface FactorICHistoryResp {
  horizon: number
  market: string
  days: number
  /** 按 trade_date 升序 */
  items: FactorICPoint[]
}

export const factorICApi = {
  evaluate: (days = 90, horizon = 5) =>
    fetchAPI<FactorICResp>(`/recommendations/strategy-factor-ic?days=${days}&horizon=${horizon}`),
  history: (horizon = 5, days = 30) =>
    fetchAPI<FactorICHistoryResp>(`/recommendations/strategy-factor-ic/history?horizon=${horizon}&days=${days}`),
}
