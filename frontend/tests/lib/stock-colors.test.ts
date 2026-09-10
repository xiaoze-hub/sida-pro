// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { hslaVar } from '@panwatch/biz-ui/lib/stock-colors'

// 2026-09-10 回归 (P1-1 热力图走查定位): ECharts/zrender 的 canvas 画笔只认
// legacy 逗号语法的 hsl()/hsla(); CSS Color 4 空格语法 `hsla(215 16% 65%, 0.18)`
// 会被 canvas fillStyle 静默忽略 (保留前一个 fillStyle) → 无数据块整片染成前一块颜色。
const LEGACY_HSL = /^hsl\(\s*\d+(\.\d+)?,\s*[\d.]+%,\s*[\d.]+%\)$/
const LEGACY_HSLA = /^hsla\(\s*\d+(\.\d+)?,\s*[\d.]+%,\s*[\d.]+%,\s*(0|1|0?\.\d+)\)$/

describe('hslaVar 输出 legacy 逗号语法 (canvas/zrender 可解析)', () => {
  it('带 alpha: 通道逗号分隔', () => {
    const out = hslaVar('--flat-color', '215 16% 57%', 0.18)
    expect(out).toMatch(LEGACY_HSLA)
    expect(out).toBe('hsla(215, 16%, 57%, 0.18)')
  })

  it('alpha=1: hsl() 同样逗号分隔', () => {
    const out = hslaVar('--foreground', '240 10% 10%')
    expect(out).toMatch(LEGACY_HSL)
    expect(out).toBe('hsl(240, 10%, 10%)')
  })

  it('变量值已是逗号形式时不重复分隔', () => {
    const out = hslaVar('--never-set-var', '215, 16%, 57%', 0.18)
    expect(out).toBe('hsla(215, 16%, 57%, 0.18)')
  })
})
