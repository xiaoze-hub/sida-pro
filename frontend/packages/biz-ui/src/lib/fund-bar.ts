/**
 * L3 资金柱(明盘 + 暗盘)的**唯一**净额/分色/时间口径(Finding 2 抽出共享)。
 *
 * 背景(真缺陷, T19): 后端 `/klines/{s}/summary`.fund_flow 的**真实字段名是 `ming_net`**
 * (明盘, 见 `src/web/api/klines.py`), 而图组件早期只读 `open_net`(后端从不下发)⇒ 明盘分量恒 0。
 * 修复的**关键**是两张图(`KlineChart` 与 `InteractiveKline`)必须共用**同一个**净额定义
 * (`net = 明盘 + 暗盘`)与**同一套**脏值护栏 —— 否则同名同源字段在两个组件里算出两种 net,
 * 正是"同源不同语义"的漂移。
 *
 * 本模块只放**纯函数**(可单测, 无 DOM/LWC 依赖):
 *  - `numOrNull`   —— 把 NaN/字符串/null/undefined 一律收敛为 null(不静默当 0);
 *  - `fundBarTime` —— 日期 → LWC 时间(秒), 日/周/月走 UTC 零点, 分钟级走本地解析;
 *  - `fundBarPoint`—— 资金柱单点 `{time, value(明盘+暗盘), color}`。
 *
 * `KlineChart.tsx` 的 `DAY_BUCKETS`/`toChartTime` 与本模块**同口径**且已收敛为**单一真源**:
 * `KlineChart` 内的 `toChartTime` 现在直接调用 `fundBarTime`, 组件内不再另存一份 `DAY_BUCKETS`。
 */
import type { Time } from 'lightweight-charts'
import { withAlpha, type StockColors } from './stock-colors'

/**
 * K 线周期(与 `KlineChart` 的 `KlineInterval` 同一联合类型)。
 * 定义在 lib 层(而非组件)以免 `lib → components → lib` 的循环 import;
 * `KlineChart.tsx` 仍 `export type KlineInterval` 转发本类型(既有调用方零改动)。
 */
export type KlineInterval = '1m' | '5m' | '15m' | '30m' | '60m' | '1d' | '1w' | '1mth'

/** 资金柱数据(对接后端 `fund_flow` 字段). 红涨绿跌 + 主净分色。 */
export interface FundFlowBar {
  date: string
  /**
   * 明盘净额 (单笔 >30 万大单, 元) —— **后端真实字段名**(`/klines/{s}/summary`.fund_flow.ming_net,
   * 见 `src/web/api/klines.py`)。历史逐日为 null(big_order_flow 仅当日), 显式无数据。
   */
  ming_net?: number | null
  /** 明盘净额的旧别名: 早期本类型误写成 `open_net`(后端从不下发) ⇒ 恒 0。保留兼容既有调用方。 */
  open_net?: number | null
  /** 暗盘净额 (.tck 委托号或 thsdk 逐笔, 元). null=无数据 */
  dark_net?: number | null
}

/**
 * 数值收敛: 非 number(NaN/字符串/null/undefined)一律返回 null, 不静默当 0。
 * 关键在 NaN —— `typeof NaN === 'number'`, 只判 `typeof` 会让 NaN 混进净额与分色比较
 * (`NaN > 0` / `NaN !== 0` 均为 false ⇒ 分色静默走错分支)。故必须 `Number.isFinite`。
 */
export function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** 日线级 K 线周期(日期粒度) —— 全程唯一真源(`KlineChart` 的 `toChartTime` 亦走此表)。 */
export const DAY_BUCKETS: readonly KlineInterval[] = ['1d', '1w', '1mth']

/**
 * 日期 → LWC 时间(秒)。日/周/月: 该日 00:00Z; 分钟级: 兼容 ISO 或 `YYYY-MM-DD HH:MM:SS`。
 * 解析不出时返回 0(LWC 对 0 容忍, 不抛), 不用当前时间兜底(那是编造时间轴)。
 */
export function fundBarTime(date: string, intv: KlineInterval): number {
  if (DAY_BUCKETS.includes(intv)) {
    const t = new Date(date.substring(0, 10) + 'T00:00:00Z').getTime() / 1000
    return Number.isFinite(t) ? t : 0
  }
  const t = new Date(date.replace(' ', 'T')).getTime() / 1000
  return Number.isFinite(t) ? t : 0
}

/**
 * L3 资金柱单点(纯函数, T19 从渲染 effect 抽出以便单测; **两图共用**)。
 *
 * 明盘优先读 `ming_net`, `open_net` 仅作旧调用方兼容兜底; 暗盘读 `dark_net`。
 * 返回 `{time, value(元, = 明盘 + 暗盘), color}`:
 *  - 有暗盘分量 → 暗盘色(实心 up/down);
 *  - 无暗盘但有明盘 → 明盘色(55% 透明度 up/down);
 *  - 两者皆无/脏值 → `nodata` 中性色, value 为 0。
 */
export function fundBarPoint(
  bar: FundFlowBar,
  interval: KlineInterval,
  sc: StockColors,
  nodataColor: string,
): { time: Time; value: number; color: string } {
  const ming = numOrNull(bar.ming_net) ?? numOrNull(bar.open_net) ?? 0
  const dark = numOrNull(bar.dark_net) ?? 0
  const net = ming + dark
  let color = nodataColor
  if (dark !== 0) {
    color = dark > 0 ? sc.up : sc.down
  } else if (ming !== 0) {
    color = ming > 0 ? withAlpha(sc.up, 0.55) : withAlpha(sc.down, 0.55)
  }
  return { time: fundBarTime(bar.date, interval) as Time, value: net, color }
}

/**
 * `InteractiveKline` 的 L3 资金柱逐日映射(纯函数, 从渲染 effect 抽出以便单测 —— Finding 2)。
 *
 * 输入 K 线日期序列 + 按日期索引的资金柱表, 输出与 K 线**逐根对齐**的 `{date, value, color}`
 * (K 线当天无资金数据 → 该根为 `null`, 由调用方过滤)。`value`/`color` 一律走 `fundBarPoint`
 * (**与 `KlineChart` 同一 net 定义**: 明盘优先 `ming_net`、`open_net` 兜底、净额 = 明盘 + 暗盘、
 * `numOrNull` 挡 NaN) —— 于是"同名同源字段两图各算一套"在结构上不可能再发生。
 *
 * 时间表示故意返回 `date` 字符串(而非秒数): `InteractiveKline` 的其余 series 用日线
 * `BusinessDay` 对象, 调用方据 `date` 自行 `parseBusinessDay` 对齐。
 */
export function capitalBarRows(
  klineDates: readonly string[],
  bars: readonly FundFlowBar[],
  sc: StockColors,
  nodataColor: string,
): Array<{ date: string; value: number; color: string } | null> {
  const byDate = new Map(bars.map((b) => [b.date, b]))
  return klineDates.map((date) => {
    const bar = byDate.get(date)
    if (!bar) return null
    const { value, color } = fundBarPoint(bar, '1d', sc, nodataColor)
    return { date, value, color }
  })
}
