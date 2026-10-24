/**
 * 四档资金结构口径（2026-10-24 新增）。
 *
 * 为什么要单独钉：`tdx_exday4`（通达信按单笔成交额四档）与 `eastmoney4`（东财按单金额归类）
 * 是**两个口径**，把前者标成后者正是本项目已经犯过一次的红线错误
 * （`DarkFundTop.tsx` 曾把 `thsdk_dde` 标成 `eastmoney4`）。
 */
import { describe, expect, it } from 'vitest'
import { caliberOf } from '@panwatch/biz-ui/components/CaliberBadge'

describe('四档口径识别', () => {
  it('tdx_exday4 归一成自己的口径，不并进 eastmoney4', () => {
    expect(caliberOf('tdx_exday4')).toBe('tdx_exday4')
    expect(caliberOf('tdx_tq_exday')).toBe('tdx_exday4')
    expect(caliberOf('tdx_exday4')).not.toBe('eastmoney4')
  })

  it('大小写与空白不影响判定', () => {
    expect(caliberOf('  TDX_EXDAY4 ')).toBe('tdx_exday4')
  })

  it('东财四档与同花顺仍各自归一', () => {
    expect(caliberOf('eastmoney4')).toBe('eastmoney4')
    expect(caliberOf('thsdk_dde')).toBe('ths')
  })

  it('认不出的口径一律 unknown，不猜', () => {
    expect(caliberOf('something_else')).toBe('unknown')
    expect(caliberOf(null)).toBe('unknown')
  })
})
