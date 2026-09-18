/**
 * 主力意图的**渲染纯逻辑**(2026-09-18 P2 补搬)。
 *
 * 从 `InteractiveKline` 原样搬出(语义一字不改), 让 `KlineChart` 也能画:
 * ① 意图箭头(buy 吸筹↑ / wash 洗盘吸筹↑ / absorb 疑似吸筹↑ / sell 派发↓ / neutral 不标);
 * ② 涨停/跌停箭头(近 60 根, ±9.8% 阈值);
 * ③ 筹码峰 + 成本带上/下沿(价位线);
 * ④ 图例文案(含"数据不足(N 笔)"的诚实占位, 不去编方向)。
 *
 * 诚实口径(与 IK 一致): `data_status === 'insufficient'` 时**不画任何意图箭头/筹码线**,
 * 图例显示"数据不足(N笔)"而不是给一个方向 —— 竞价/开盘初期数据不够就该说不够。
 */
import { withAlpha } from './stock-colors'
import type { MainIntentStructured } from './main-intent-types'

/** 意图箭头阈值/配色(从 IK 逐字沿用) */
export const INTENT_COLORS = {
  buy: 'var(--stock-up)',
  wash: '#f97316',
  absorb: '#f59e0b',
  sell: 'var(--stock-down)',
} as const

/** 近 N 根内标记涨停/跌停 */
export const LIMIT_MOVE_LOOKBACK = 60
/** 涨跌幅阈值(%), IK 原值 */
export const LIMIT_MOVE_PCT = 9.8

export interface IntentLabel {
  text: string
  cls: string
}

/**
 * 图例文案。`data_status === 'insufficient'` 优先于方向 —— 数据不够就不给结论。
 */
export function intentLabelFor(mi: MainIntentStructured | null | undefined): IntentLabel | null {
  if (!mi) return null
  if (mi.data_status === 'insufficient') {
    return { text: `数据不足(${mi.tick_count ?? 0}笔)`, cls: 'text-muted-foreground font-medium' }
  }
  switch (mi.direction) {
    case 'buy':
      return { text: '吸筹↑', cls: 'text-stock-up font-medium' }
    case 'wash':
      return { text: '洗盘吸筹↑', cls: 'text-orange-500 font-medium' }
    case 'absorb':
      return { text: '疑似吸筹↑', cls: 'text-amber-500 font-medium' }
    case 'sell':
      return { text: '派发↓', cls: 'text-stock-down font-medium' }
    default:
      return { text: '平衡→', cls: 'text-muted-foreground font-medium' }
  }
}

/** 数据不足时不该画任何意图标记/筹码线(与 IK 同) */
export function intentRenderable(mi: MainIntentStructured | null | undefined): boolean {
  return !!mi && mi.data_status !== 'insufficient'
}

export interface IntentMarker {
  time: unknown
  position: 'aboveBar' | 'belowBar'
  color: string
  shape: 'arrowUp' | 'arrowDown'
  text: string
  size: number
}

/**
 * 意图箭头(只标在**最后一根** K 线上)。`toTime` 由调用方注入(把 'YYYY-MM-DD' 转成图表 Time)。
 * neutral → 不标(原 IK 行为)。
 */
export function intentMarkersFor(
  mi: MainIntentStructured | null | undefined,
  lastBarTime: unknown,
  upColor: string,
  downColor: string,
): IntentMarker[] {
  if (!intentRenderable(mi) || lastBarTime == null) return []
  const d = mi!.direction
  if (d === 'buy') {
    return [
      { time: lastBarTime, position: 'belowBar', color: upColor, shape: 'arrowUp', text: '主力吸筹', size: 1 },
    ]
  }
  if (d === 'wash') {
    return [
      {
        time: lastBarTime,
        position: 'belowBar',
        color: INTENT_COLORS.wash,
        shape: 'arrowUp',
        text: '洗盘吸筹',
        size: 1,
      },
    ]
  }
  if (d === 'absorb') {
    return [
      {
        time: lastBarTime,
        position: 'belowBar',
        color: INTENT_COLORS.absorb,
        shape: 'arrowUp',
        text: '疑似吸筹',
        size: 1,
      },
    ]
  }
  if (d === 'sell') {
    return [
      { time: lastBarTime, position: 'aboveBar', color: downColor, shape: 'arrowDown', text: '主力派发', size: 1 },
    ]
  }
  return []
}

export interface LimitMarker {
  time: unknown
  position: 'aboveBar' | 'belowBar'
  color: string
  shape: 'arrowUp' | 'arrowDown'
  text: string
  size: number
}

/**
 * 涨停/跌停箭头(近 `LIMIT_MOVE_LOOKBACK` 根)。阈值与 IK 一致: |涨跌幅| ≥ 9.8%。
 * 只标真实发生过的涨跌停, 不做预测。
 */
export function limitMoveMarkers(
  klines: Array<{ date: string; close: number }>,
  toTime: (date: string) => unknown,
  upColor: string,
  downColor: string,
): LimitMarker[] {
  const out: LimitMarker[] = []
  const from = Math.max(0, klines.length - LIMIT_MOVE_LOOKBACK)
  for (let i = Math.max(1, from); i < klines.length; i++) {
    const k = klines[i]
    const prev = klines[i - 1]
    if (!prev || !prev.close) continue
    const chg = ((k.close - prev.close) / prev.close) * 100
    const t = toTime(k.date)
    if (t == null) continue
    if (chg >= LIMIT_MOVE_PCT) {
      out.push({ time: t, position: 'belowBar', color: withAlpha(upColor, 0.9), shape: 'arrowUp', text: '涨停', size: 0 })
    } else if (chg <= -LIMIT_MOVE_PCT) {
      out.push({ time: t, position: 'aboveBar', color: withAlpha(downColor, 0.9), shape: 'arrowDown', text: '跌停', size: 0 })
    }
  }
  return out
}

export interface IntentPriceLine {
  price: number
  color: string
  /** 与 lightweight-charts 的 LineWidth 对齐(1..4), 免得调用方还要断言 */
  lineWidth: 1 | 2 | 3 | 4
  /** lightweight-charts LineStyle: 0 实线 / 1 点 / 2 虚 / 3 点划 */
  lineStyle: 0 | 1 | 2 | 3 | 4
  title: string
  axisLabelVisible: boolean
}

/** 筹码峰 + 成本带上/下沿(与 IK 同色同线型) */
export function intentPriceLinesFor(mi: MainIntentStructured | null | undefined): IntentPriceLine[] {
  if (!intentRenderable(mi)) return []
  const out: IntentPriceLine[] = []
  if (mi!.chip_peak != null) {
    out.push({
      price: mi!.chip_peak,
      color: 'rgba(234, 179, 8, 0.6)',
      lineWidth: 1,
      lineStyle: 1,
      title: '筹码峰',
      axisLabelVisible: true,
    })
  }
  if (mi!.chip_band) {
    out.push({
      price: mi!.chip_band.high,
      color: 'rgba(148, 163, 184, 0.45)',
      lineWidth: 1,
      lineStyle: 3,
      title: '成本上沿',
      axisLabelVisible: true,
    })
    out.push({
      price: mi!.chip_band.low,
      color: 'rgba(148, 163, 184, 0.45)',
      lineWidth: 1,
      lineStyle: 3,
      title: '成本下沿',
      axisLabelVisible: true,
    })
  }
  return out
}
