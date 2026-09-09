// KI-037 收口: 前端指标与后端 `src/core/indicators.py` 逐值对齐。
// 夹具由 `scripts/gen_indicators_parity_fixture.py` 用后端实现生成(容差 1e-9)。
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import {
  macd,
  rsiCutlerSeries,
  smaSeries,
  emaSeries,
} from '../../packages/biz-ui/src/lib/indicators'

interface Fixture {
  meta: { tolerance: number }
  closes: number[]
  expected: {
    sma: Record<string, Array<number | null>>
    ema: Record<string, Array<number | null>>
    macd: { dif: Array<number | null>; dea: Array<number | null>; hist: Array<number | null> }
    rsi6: Array<number | null>
  }
}

const fixture = JSON.parse(
  readFileSync(new URL('../fixtures/indicators_parity.json', import.meta.url), 'utf-8'),
) as Fixture
const TOL = fixture.meta.tolerance ?? 1e-9

function expectSeriesClose(actual: Array<number | null>, expected: Array<number | null>) {
  expect(actual.length).toBe(expected.length)
  for (let i = 0; i < expected.length; i++) {
    const e = expected[i]
    const a = actual[i]
    if (e == null) {
      expect(a).toBeNull()
      continue
    }
    expect(a).not.toBeNull()
    expect(Math.abs((a as number) - e)).toBeLessThanOrEqual(TOL)
  }
}

describe('指标前后端 parity (KI-037)', () => {
  it('SMA 5/10/20/60 与后端逐值一致', () => {
    for (const [period, expected] of Object.entries(fixture.expected.sma)) {
      expectSeriesClose(smaSeries(fixture.closes, Number(period)), expected)
    }
  })

  it('EMA 12/26 与后端逐值一致(首值播种)', () => {
    for (const [period, expected] of Object.entries(fixture.expected.ema)) {
      expectSeriesClose(emaSeries(fixture.closes, Number(period)), expected)
    }
  })

  it('MACD DIF/DEA/HIST 与后端逐值一致(HIST 已含 ×2)', () => {
    const out = macd(fixture.closes)
    expectSeriesClose(out.dif, fixture.expected.macd.dif)
    expectSeriesClose(out.dea, fixture.expected.macd.dea)
    expectSeriesClose(out.hist, fixture.expected.macd.hist)
  })

  it('RSI6 与后端逐值一致(Cutler 简单均值口径)', () => {
    expectSeriesClose(rsiCutlerSeries(fixture.closes, 6), fixture.expected.rsi6)
  })
})
