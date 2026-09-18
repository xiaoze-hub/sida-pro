/**
 * 主力意图结构化数据类型 · 单一来源(2026-09-18)。
 *
 * 原先定义在 `InteractiveKline.tsx` 里, 被 `insight/types.ts`、`insight/useInsightData.ts`、
 * `workbench/tabs/L2Tab.tsx` 以及 IK 自身引用 —— 淘汰 IK 必须先把类型搬出来, 否则连坐。
 *
 * 形状**逐字段照抄自 IK**(搬迁不顺手"优化", 免得变成破坏性改动)。
 */

/** 主力意图结构化数据(2026-08-12): 后端 `klines/{symbol}/summary` 返回, 供 K线 markers/筹码叠加 */
export interface MainIntentStructured {
  direction: 'buy' | 'sell' | 'neutral' | 'wash' | 'absorb'
  main_net: number
  big_net?: number
  mid_net?: number
  participation?: number | null
  buy_ratio?: number | null
  auction_amt?: number
  phase?: string | null
  signal?: string | null
  chip_peak?: number | null
  chip_band?: { low: number; high: number } | null
  profit_ratio?: number | null
  tail_net?: number
  /** 2026-08-12: 竞价/开盘初期数据不足标记 */
  data_status?: 'ok' | 'insufficient'
  tick_count?: number
}
