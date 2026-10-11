/**
 * 决策先锋「暗盘资金」副图(规格 §2)纯逻辑(2026-10-11)。
 *
 * 数据源: `GET /klines/{symbol}/pool-flow-series`(后端 `dark_pool_flow.compute_pool_flow_series`)。
 *   - 历史日 = 暗盘 OHLC 分摊近似(方向/符号可靠, 幅度有偏差) → 逐点 `approximation=true`;
 *   - 当日(末根)若权威 ming+dark 可得则已叠加(`approximation=false`)。
 * 图上必须**显式标注** `L1_approx`(诚实: 不把近似当完整口径)。
 *
 * 本模块只放纯函数:
 *   - `DARK_POOL_CONFIDENCE` —— 图例小字用;
 *   - `darkPoolBarPoint`     —— 单点 → 红(流入)/绿(流出)柱; 0/null 不画(不冒充);
 *   - `darkPoolCombo`        —— 涨跌 × 红绿 的四组合用法(规格 §2, 悬停读数用)。
 */
import type { Time } from 'lightweight-charts'
import { fundBarTime, numOrNull, type KlineInterval } from './fund-bar'
import type { StockColors } from './stock-colors'

/** 历史日暗盘 = OHLC 分摊近似(与后端 `dark_pool_flow.DARK_CONFIDENCE` 同值)。 */
export const DARK_POOL_CONFIDENCE = 'L1_approx'

export interface DarkPoolPoint {
  date: string
  /** 主力净额(元); null=无数据 */
  main_net?: number | null
  /** true=历史日 OHLC 近似; false=当日权威 ming+dark */
  approximation?: boolean
  source?: string | null
}

/**
 * 单点 → 副图柱 `{time, value, color}`:
 *   红柱=流入(net>0) / 绿柱=流出(net<0)。
 * net 为 0 或脏值(null/NaN) → 返回 `null`(该根不画, 绝不把 0 冒充成\"平衡柱\")。
 */
export function darkPoolBarPoint(
  p: DarkPoolPoint,
  interval: KlineInterval,
  sc: StockColors,
): { time: Time; value: number; color: string } | null {
  const net = numOrNull(p.main_net)
  if (net === null || net === 0) return null
  return {
    time: fundBarTime(p.date, interval) as Time,
    value: net,
    color: net > 0 ? sc.up : sc.down,
  }
}

/**
 * 四组合用法(规格 §2): 据当日涨跌(收盘 vs 前收) × 暗盘柱红绿, 给一句**用法提示**。
 * 证据口径, 非买卖建议(措辞与规格原文一致)。
 * 涨跌或净额任一缺失 → null(不猜)。
 */
export function darkPoolCombo(closeChange: number | null, net: number | null): string | null {
  if (closeChange === null || net === null || closeChange === 0 || net === 0) return null
  const up = closeChange > 0
  const red = net > 0
  if (up && red) return '涨+红柱 · 持股待涨'
  if (!up && !red) return '跌+绿柱 · 回避减仓'
  if (!up && red) return '跌+红柱 · 底部吸筹(重点关注)'
  return '涨+绿柱 · 诱多警惕(减仓)'
}
