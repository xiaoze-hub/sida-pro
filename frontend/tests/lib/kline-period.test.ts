// 2026-09-18 设计稿 v2.1 §10.2① —— K 线周期 ↔ URL `?period=` 映射。
//
// 钉住三件事:
//  ① 设计稿规定的 8 个 K 线取值(除 intra)双向可逆, 不出现"写进去读不回来";
//  ② `intra`(分时)不在本表 —— 分时是 MinuteLwcChart 的活, 这里必须返回 undefined
//     让调用方落回默认周期, 而不是把它悄悄当某个 K 线周期用;
//  ③ 非法/缺失/大小写混写一律安全(不抛、不乱猜)。
import { describe, expect, it } from 'vitest'
import {
  intervalToPeriod,
  isSupportedPeriod,
  periodToInterval,
} from '@/lib/kline-period'

describe('periodToInterval', () => {
  it('设计稿 8 个 K 线取值全部认得', () => {
    expect(periodToInterval('m1')).toBe('1m')
    expect(periodToInterval('m5')).toBe('5m')
    expect(periodToInterval('m15')).toBe('15m')
    expect(periodToInterval('m30')).toBe('30m')
    expect(periodToInterval('m60')).toBe('60m')
    expect(periodToInterval('d1')).toBe('1d')
    expect(periodToInterval('w1')).toBe('1w')
    expect(periodToInterval('mn')).toBe('1mth')
  })

  it('intra(分时)不是 K 线周期 → undefined(不假装支持)', () => {
    expect(periodToInterval('intra')).toBeUndefined()
    expect(isSupportedPeriod('intra')).toBe(false)
  })

  it('缺失/空/非法值 → undefined, 不回退成随便一个周期', () => {
    expect(periodToInterval(null)).toBeUndefined()
    expect(periodToInterval(undefined)).toBeUndefined()
    expect(periodToInterval('')).toBeUndefined()
    expect(periodToInterval('1d')).toBeUndefined() // 图表字面量('1d')不是 URL 取值('d1')
    expect(periodToInterval('1w')).toBeUndefined() // 同理: URL 侧是 'w1'
    expect(periodToInterval('intra')).toBeUndefined()
  })

  it('大小写与空白容错(链接可能被人手改过)', () => {
    expect(periodToInterval('  D1 ')).toBe('1d')
    expect(periodToInterval('MN')).toBe('1mth')
  })
})

describe('intervalToPeriod', () => {
  it('周期 → URL 取值, 与正表一一对应', () => {
    expect(intervalToPeriod('1d')).toBe('d1')
    expect(intervalToPeriod('1mth')).toBe('mn')
    expect(intervalToPeriod('1m')).toBe('m1')
  })

  it('往返一致(写进 URL 再读回来必须是同一个周期)', () => {
    const intervals = ['1m', '5m', '15m', '30m', '60m', '1d', '1w', '1mth'] as const
    for (const i of intervals) {
      expect(periodToInterval(intervalToPeriod(i))).toBe(i)
    }
  })

  it('不支持的周期(1w 之外的字面量)不返回 undefined 而是安全值', () => {
    // 联合类型收口, 但运行时(旧链接/脏数据)可能传进未知值 → 回 d1, 不抛
    expect(intervalToPeriod('bogus' as never)).toBe('d1')
  })
})

describe('isSupportedPeriod', () => {
  it('同时覆盖"支持/不支持"两侧', () => {
    expect(isSupportedPeriod('m15')).toBe(true)
    expect(isSupportedPeriod('d1')).toBe(true)
    expect(isSupportedPeriod('1d')).toBe(false)
    expect(isSupportedPeriod(null)).toBe(false)
  })
})
