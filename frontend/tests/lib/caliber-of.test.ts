// 口径映射契约(2026-10-24 P0): 上游 source 常写 `thsdk_dde` / `thsdk_alert` 等具体通道名,
// 不是裸 `ths`。caliberOf() 若只做精确匹配, 这些值全被归成 unknown, 调用方随后容易
// 「凑」一个 eastmoney4 顶上 —— DarkFundTop / WarReport 的口径错标就是这么来的。
// 本文件钉住: thsdk* → ths, 且其他口径不被顺手改写(四口径仍不可换算)。
import { describe, expect, it } from 'vitest'
import { caliberOf } from '@sida/biz-ui/components/CaliberBadge'

describe('caliberOf 口径归类', () => {
  it('thsdk* 前缀一律归为 ths(同花顺口径), 不再落到 unknown', () => {
    expect(caliberOf('thsdk_dde')).toBe('ths')
    expect(caliberOf('thsdk')).toBe('ths')
    expect(caliberOf('thsdk_alert')).toBe('ths')
    expect(caliberOf('thsdk_tick')).toBe('ths')
    expect(caliberOf('ths_auction')).toBe('ths')
  })

  it('大小写 / 空白不改变归类', () => {
    expect(caliberOf('  THSDK_DDE  ')).toBe('ths')
    expect(caliberOf('Ths')).toBe('ths')
  })

  it('三口径仍各自独立, 不被 ths 前缀规则吞掉', () => {
    expect(caliberOf('tick')).toBe('tick')
    expect(caliberOf('eastmoney4')).toBe('eastmoney4')
    expect(caliberOf('ths')).toBe('ths')
  })

  it('拿不到标签一律 unknown(不猜)', () => {
    expect(caliberOf(null)).toBe('unknown')
    expect(caliberOf(undefined)).toBe('unknown')
    expect(caliberOf('')).toBe('unknown')
    expect(caliberOf('dde')).toBe('unknown')
    expect(caliberOf('supamo')).toBe('unknown')
  })
})
