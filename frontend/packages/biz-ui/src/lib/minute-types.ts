/**
 * 分时(分钟线)数据类型 · 单一来源(2026-09-18 P1)。
 *
 * 为什么单独成文件: 这些类型原先定义在 `InteractiveKline.tsx` 里, 而 `MinuteLwcChart`
 * 反过来 `import type { MinuteSwings } from './InteractiveKline'` —— 于是"分钟线组件"
 * 与"旧 K 线组件"形成**类型耦合**, 一旦按评估淘汰 IK 就会连带打断 MinuteLwcChart。
 * 提到 lib 后: IK / MinuteLwcChart / KlineChart 都从这里取, 谁退役都不影响别人。
 *
 * 形状**逐字段照抄自 InteractiveKline**(不许在搬迁时顺手"优化"——那会变成破坏性改动)。
 *
 * 单位口径(延续全仓约定): price/avg = 元, volume = 股, 金额字段 = 元; 缺失用 `null`/缺省,
 * **不补 0**(0 是真实数值, 不能冒充"没有数据")。
 */

/** 分时点: 某一分钟的价格/均价/成交量(股) */
export type MinutePoint = {
  t: string
  price: number
  avg: number
  volume: number
}

/** 拉升/下探段(2026-08-12): 后端 minute 接口 swings 字段, 供分时K区间标记 */
export interface SwingSegment {
  start: string
  end: string
  price_up?: number
  price_down?: number
  amt: number
  main_net: number
  main_buy?: number
  main_sell?: number
  retail_net: number
  retail_buy?: number
  retail_sell?: number
  buy_ratio?: number
  sell_ratio?: number
  verdict: string
  score: number
  signals?: string[]
  post?: { main_net: number; price_change: number } | null
  spread?: number
}

/** 分时波段汇总 */
export interface MinuteSwings {
  symbol: string
  current_price: number
  rallies: SwingSegment[]
  dips: SwingSegment[]
  flats?: SwingSegment[]
  summary?: {
    n_rallies?: number
    n_dips?: number
    true_rallies?: number
    true_dips?: number
    main_net_total?: number
  }
}

/** `GET /quotes/minute/{symbol}` 响应 */
export type MinuteResponse = {
  symbol: string
  market: string
  points: MinutePoint[]
  prev_close?: number | null
  is_index?: boolean
  swings?: MinuteSwings | null
  /** KI-042: true = 分时源故障(空列表是故障, 不是停牌); note 为后端原文。 */
  degraded?: boolean
  note?: string | null
}
