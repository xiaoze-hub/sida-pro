/**
 * 技术指标统一实现 (KI-037 收口, 2026-09-09)。
 *
 * 口径与后端 `src/core/indicators.py` **逐值对齐**(容差 1e-9), 由
 * `frontend/tests/lib/indicators-parity.test.ts` 用后端生成的夹具
 * (`frontend/tests/fixtures/indicators_parity.json`) 锁定。
 *
 * 口径备忘:
 * - SMA: 最近 period 个的简单均值
 * - EMA: `values[0]` 播种, k = 2/(period+1)
 * - MACD: DIF = EMA(fast) − EMA(slow); DEA = EMA(DIF, signal); HIST = (DIF − DEA) × 2
 * - RSI: Cutler 简单均值(非 Wilder; 后端另有 rsi_wilder 供研究对照)
 *
 * 数据不足返回 null / null 填充, 不抛异常(与后端 fail-soft 一致)。
 */

/** 简单移动平均序列(不足 period 返回 null)。 */
export function smaSeries(values: number[], period: number): Array<number | null> {
  if (period <= 1) return values.map((v) => v)
  const out: Array<number | null> = new Array(values.length).fill(null)
  let sum = 0
  for (let i = 0; i < values.length; i++) {
    sum += values[i]
    if (i >= period) sum -= values[i - period]
    if (i >= period - 1) out[i] = sum / period
  }
  return out
}

/** EMA 序列(首值播种, k = 2/(period+1))。 */
export function emaSeries(values: number[], period: number): Array<number | null> {
  const out: Array<number | null> = new Array(values.length).fill(null)
  if (values.length === 0 || period <= 0) return out
  const k = 2 / (period + 1)
  let prev: number | null = null
  for (let i = 0; i < values.length; i++) {
    const v = values[i]
    if (prev == null) {
      prev = v
      out[i] = v
      continue
    }
    prev = v * k + prev * (1 - k)
    out[i] = prev
  }
  return out
}

export interface MacdSeries {
  dif: Array<number | null>
  dea: Array<number | null>
  hist: Array<number | null>
}

/**
 * MACD 全序列(DIF/DEA/HIST)。
 * HIST = (DIF − DEA) × 2 —— 与后端 `indicators.macd` 一致(旧前端漏乘 2, KI-037)。
 */
export function macd(closes: number[], fast = 12, slow = 26, signal = 9): MacdSeries {
  const emaFast = emaSeries(closes, fast)
  const emaSlow = emaSeries(closes, slow)
  const dif: Array<number | null> = closes.map((_, i) => {
    const a = emaFast[i]
    const b = emaSlow[i]
    if (a == null || b == null) return null
    return a - b
  })
  const difVals = dif.map((v) => (v == null ? 0 : v))
  const dea = emaSeries(difVals, signal)
  const hist: Array<number | null> = dif.map((v, i) => {
    const d = dea[i]
    if (v == null || d == null) return null
    return (v - d) * 2
  })
  return { dif, dea, hist }
}

/**
 * RSI 序列(Cutler 简单均值口径, 与后端 `indicators.rsi` 一致)。
 * 第 i 个值 = 前 period 个涨跌幅的简单均值口径; i < period 为 null。
 */
export function rsiCutlerSeries(closes: number[], period: number): Array<number | null> {
  const out: Array<number | null> = new Array(closes.length).fill(null)
  if (period <= 0 || closes.length < period + 1) return out
  const gains: number[] = []
  const losses: number[] = []
  for (let i = 1; i < closes.length; i++) {
    const change = closes[i] - closes[i - 1]
    if (change > 0) {
      gains.push(change)
      losses.push(0)
    } else {
      gains.push(0)
      losses.push(Math.abs(change))
    }
    if (i < period) continue
    const g = gains.slice(-period).reduce((a, b) => a + b, 0) / period
    const l = losses.slice(-period).reduce((a, b) => a + b, 0) / period
    out[i] = l === 0 ? 100 : 100 - 100 / (1 + g / l)
  }
  return out
}
