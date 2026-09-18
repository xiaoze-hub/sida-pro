// P2 补搬(2026-09-18): 主力意图的渲染纯逻辑单测。
//
// 为什么值得单独测: 这段是从 InteractiveKline 搬过来的**用户可见**能力(箭头/筹码线/图例),
// 且带一条诚实口径 —— 数据不足时**不给方向**(显示"数据不足(N笔)")。搬迁最容易悄悄改的就是这类分支。
import { describe, expect, it } from 'vitest'

import {
  LIMIT_MOVE_LOOKBACK,
  LIMIT_MOVE_PCT,
  intentLabelFor,
  intentMarkersFor,
  intentPriceLinesFor,
  intentRenderable,
  limitMoveMarkers,
  type IntentLabel,
} from '@panwatch/biz-ui/lib/main-intent'
import type { MainIntentStructured } from '@panwatch/biz-ui/lib/main-intent-types'

const UP = '#E53935'
const DOWN = '#43A047'

const mi = (over: Partial<MainIntentStructured>): MainIntentStructured =>
  ({ direction: 'neutral', main_net: 0, ...over }) as MainIntentStructured

describe('图例文案(含"数据不足"的诚实占位)', () => {
  it('数据不足优先于方向, 显示笔数而不是编一个方向', () => {
    const got = intentLabelFor(mi({ direction: 'buy', data_status: 'insufficient', tick_count: 42 })) as IntentLabel
    expect(got.text).toBe('数据不足(42笔)')
    expect(intentRenderable(mi({ direction: 'buy', data_status: 'insufficient' }))).toBe(false)
  })

  it('五个方向各有对应文案', () => {
    expect(intentLabelFor(mi({ direction: 'buy' }))?.text).toBe('吸筹↑')
    expect(intentLabelFor(mi({ direction: 'wash' }))?.text).toBe('洗盘吸筹↑')
    expect(intentLabelFor(mi({ direction: 'absorb' }))?.text).toBe('疑似吸筹↑')
    expect(intentLabelFor(mi({ direction: 'sell' }))?.text).toBe('派发↓')
    expect(intentLabelFor(mi({ direction: 'neutral' }))?.text).toBe('平衡→')
  })

  it('没有数据 → 不给图例', () => {
    expect(intentLabelFor(null)).toBeNull()
    expect(intentLabelFor(undefined)).toBeNull()
  })
})

describe('意图箭头', () => {
  it('buy → 下方红箭"主力吸筹"; sell → 上方绿箭"主力派发"', () => {
    const buy = intentMarkersFor(mi({ direction: 'buy' }), 'T', UP, DOWN)
    expect(buy).toHaveLength(1)
    expect(buy[0]).toMatchObject({ position: 'belowBar', color: UP, text: '主力吸筹' })
    const sell = intentMarkersFor(mi({ direction: 'sell' }), 'T', UP, DOWN)
    expect(sell[0]).toMatchObject({ position: 'aboveBar', color: DOWN, text: '主力派发' })
  })

  it('wash / absorb 用各自配色', () => {
    expect(intentMarkersFor(mi({ direction: 'wash' }), 'T', UP, DOWN)[0].text).toBe('洗盘吸筹')
    expect(intentMarkersFor(mi({ direction: 'absorb' }), 'T', UP, DOWN)[0].text).toBe('疑似吸筹')
  })

  it('neutral / 数据不足 / 无数据 → 不画箭头', () => {
    expect(intentMarkersFor(mi({ direction: 'neutral' }), 'T', UP, DOWN)).toHaveLength(0)
    expect(intentMarkersFor(mi({ direction: 'buy', data_status: 'insufficient' }), 'T', UP, DOWN)).toHaveLength(0)
    expect(intentMarkersFor(null, 'T', UP, DOWN)).toHaveLength(0)
    // 没有 K 线时间也不画(避免给空图喂脏点)
    expect(intentMarkersFor(mi({ direction: 'buy' }), null, UP, DOWN)).toHaveLength(0)
  })
})

describe('涨停/跌停箭头', () => {
  const k = (date: string, close: number) => ({ date, close })
  const toTime = (d: string) => d

  it('≥9.8% 标涨停(红), ≤-9.8% 标跌停(绿), 其余不标', () => {
    const bars = [k('d1', 10), k('d2', 11), k('d3', 11.1), k('d4', 9.99)]
    const got = limitMoveMarkers(bars, toTime, UP, DOWN)
    expect(got.map((m) => m.text)).toEqual(['涨停', '跌停'])
    expect(got[0].shape).toBe('arrowUp')
    expect(got[1].shape).toBe('arrowDown')
  })

  it('刚好 9.8% 也算(阈值含等号, 与 IK 一致)', () => {
    const bars = [k('d1', 10), k('d2', 10.98)]
    expect(limitMoveMarkers(bars, toTime, UP, DOWN)).toHaveLength(1)
    expect(LIMIT_MOVE_PCT).toBe(9.8)
  })

  it('只看近 N 根, 并跳过首根(没有前收)', () => {
    const bars = Array.from({ length: LIMIT_MOVE_LOOKBACK + 10 }, (_, i) => k(`d${i}`, 10))
    expect(limitMoveMarkers(bars, toTime, UP, DOWN)).toHaveLength(0)
    expect(limitMoveMarkers([k('d1', 10)], toTime, UP, DOWN)).toHaveLength(0)
  })
})

describe('筹码峰 / 成本带价位线', () => {
  it('有筹码峰 → 一条"筹码峰"线; 有成本带 → 上下沿两条', () => {
    const lines = intentPriceLinesFor(
      mi({ direction: 'buy', chip_peak: 9.87, chip_band: { low: 9.1, high: 10.4 } }),
    )
    expect(lines.map((l) => l.title)).toEqual(['筹码峰', '成本上沿', '成本下沿'])
    expect(lines[0].price).toBe(9.87)
    expect(lines[1].price).toBe(10.4)
  })

  it('缺失就不画(不补 0 线)', () => {
    expect(intentPriceLinesFor(mi({ direction: 'buy' }))).toHaveLength(0)
    expect(intentPriceLinesFor(mi({ direction: 'buy', data_status: 'insufficient', chip_peak: 9.9 }))).toHaveLength(0)
  })
})
