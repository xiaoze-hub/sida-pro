/**
 * v2.1 §10.2④ 区间统计 —— 可视区间的反查读数(纯函数, 可单测, 无 DOM/LWC 依赖)。
 *
 * 设计稿要求: 用户在 K 线大图上拖拽选段(或缩放)后, 资金面板顶部多出"区间统计"一行:
 *   区间首末价格 / 涨跌幅 / 振幅, 区间累计明盘净额(流入红/流出绿), 区间累计暗盘净额,
 *   区间事件数量(涨停/拆单/撤单/托压 各几次)。
 *
 * 口径与诚实约束(与全仓"缺数据不补 0"纪律一致):
 *  - 区间内**一根 K 线都没有** → 返回 `null`(调用方清空该行, 不渲染空壳)。
 *  - 明盘/暗盘累计只对**非 null** 的日求和, 并同时给出**有值天数**(`mingDays`/`darkDays`);
 *    `mingDays === 0` 时累计值必须是 `null` 而不是 `0` —— 0 是"净额为 0", null 是"没有数据",
 *    两者混淆正是设计稿点名要防的"编造数字"。
 *  - 资金柱/事件按**日期**落区间(与 K 线同一日期集合), 不打时间轴近似。
 *
 * 为什么按日期而非时间戳落区间: K 线是 1d/1w/1mth/分钟级混合粒度, 而后端 `fund_flow` 与
 * `events` 只有日期粒度。用日期集合求交, 两种粒度下都不会把区间外的数据算进来。
 */

import type { KlineEventPoint, KlinePriceLine } from '../klineEvents'
import type { FundFlowBar } from './fund-bar'

/** 图表侧一根 K 线(已归一化: time 为 LWC 时间戳(秒), date 为 'YYYY-MM-DD')。 */
export interface RangeBar {
  time: number
  date: string
  open: number
  high: number
  low: number
  close: number
}

/** 区间统计结果 —— 每个字段都可能为 null(显式无数据), 消费方一律显示 `--`。 */
export interface KlineRangeStats {
  /** 区间首根 K 线日期('YYYY-MM-DD') */
  from: string
  /** 区间末根 K 线日期 */
  to: string
  /** 区间内 K 线根数(>0; 为 0 时本函数返回 null) */
  bars: number
  /** 首根收盘价 */
  firstClose: number
  /** 末根收盘价 */
  lastClose: number
  /** 涨跌幅 % = (末收 − 首收) / 首收 × 100 */
  changePct: number
  /** 振幅 % = (区间最高 − 区间最低) / 区间最低 × 100 */
  amplitudePct: number
  /** 区间累计明盘净额(元); null = 区间内无任何明盘数据 */
  mingNet: number | null
  /** 明盘有值天数(0 表示整段缺数, 此时 mingNet 必为 null) */
  mingDays: number
  /** 区间累计暗盘净额(元); null = 区间内无任何暗盘数据 */
  darkNet: number | null
  /** 暗盘有值天数 */
  darkDays: number
  /** 区间事件计数: kind → 次数(只统计白名单内且落在区间的) */
  eventCounts: Record<string, number>
  /** 区间事件总数(等价于 eventCounts 求和, 便于一行展示) */
  eventTotal: number
  /** 区间内出现过的价位线(支撑/压力, 按价格落区间判定); 无数据 → [] */
  priceLines: KlinePriceLine[]
}

function finite(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** 明盘净额: 后端真实字段 `ming_net`; 早期别名 `open_net` 兼容(后者从不下发)。 */
function mingOf(bar: FundFlowBar): number | null {
  return finite(bar.ming_net) ?? finite(bar.open_net)
}

/**
 * 计算区间统计。
 *
 * @param bars      已加载的 K 线(升序, time=秒)
 * @param fundFlow  日级资金柱序列(可空)
 * @param events    事件点序列(可空)
 * @param priceLines 价位线序列(可空; 按"价格落在区间高低之间"纳入)
 * @param from      可视化区间起点(LWC 时间戳, 秒)
 * @param to        可视化区间终点(LWC 时间戳, 秒)
 */
export function computeRangeStats(
  bars: ReadonlyArray<RangeBar>,
  fundFlow: ReadonlyArray<FundFlowBar> | null | undefined,
  events: ReadonlyArray<KlineEventPoint> | null | undefined,
  priceLines: ReadonlyArray<KlinePriceLine> | null | undefined,
  from: number,
  to: number,
): KlineRangeStats | null {
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null
  const lo = Math.min(from, to)
  const hi = Math.max(from, to)
  const sel = (bars || []).filter((b) => Number.isFinite(b.time) && b.time >= lo && b.time <= hi)
  if (sel.length === 0) return null

  const first = sel[0]
  const last = sel[sel.length - 1]
  let hiPrice = first.high
  let loPrice = first.low
  for (const b of sel) {
    if (b.high > hiPrice) hiPrice = b.high
    if (b.low < loPrice) loPrice = b.low
  }
  const changePct = first.close > 0 ? ((last.close - first.close) / first.close) * 100 : 0
  const amplitudePct = loPrice > 0 ? ((hiPrice - loPrice) / loPrice) * 100 : 0

  // 资金柱 / 事件: 按"区间 K 线日期集合"求交(不按时间戳近似)
  const dateSet = new Set(sel.map((b) => b.date))
  let mingNet: number | null = null
  let mingDays = 0
  let darkNet: number | null = null
  let darkDays = 0
  for (const f of fundFlow || []) {
    if (!f || typeof f.date !== 'string' || !dateSet.has(f.date.slice(0, 10))) continue
    const m = mingOf(f)
    if (m !== null) {
      mingNet = (mingNet ?? 0) + m
      mingDays += 1
    }
    const d = finite(f.dark_net)
    if (d !== null) {
      darkNet = (darkNet ?? 0) + d
      darkDays += 1
    }
  }

  const eventCounts: Record<string, number> = {}
  let eventTotal = 0
  for (const e of events || []) {
    if (!e || typeof e.date !== 'string') continue
    if (!dateSet.has(e.date.slice(0, 10))) continue
    eventCounts[e.kind] = (eventCounts[e.kind] || 0) + 1
    eventTotal += 1
  }

  const inRangeLines = (priceLines || []).filter(
    (l) => l && Number.isFinite(l.price) && l.price >= loPrice && l.price <= hiPrice,
  )

  return {
    from: first.date,
    to: last.date,
    bars: sel.length,
    firstClose: first.close,
    lastClose: last.close,
    changePct,
    amplitudePct,
    mingNet,
    mingDays,
    darkNet,
    darkDays,
    eventCounts,
    eventTotal,
    priceLines: inRangeLines.slice(),
  }
}
