/**
 * OKLCH → sRGB 转换(2026-09-20 强度阶落地的基础设施)。
 * 判据: 端点必须准(白/黑/纯色的已知值), 越界必须裁剪而不是给 NaN(画布上 NaN = 整块不画)。
 */
import { describe, expect, it } from 'vitest'
import { oklchStringToHex, oklchToRgb, parseOklch, rgbToHex } from '@panwatch/biz-ui/lib/oklch'

describe('parseOklch', () => {
  it('解析 oklch(L C H) 与百分号写法; 解析不了返回 null(不猜)', () => {
    expect(parseOklch('oklch(0.62 0.13 25)')).toEqual({ L: 0.62, C: 0.13, H: 25 })
    expect(parseOklch('oklch(62% 0.13 25)')!.L).toBeCloseTo(0.62, 5)
    expect(parseOklch('oklch(0.62 0.13 25 / 0.5)')).toEqual({ L: 0.62, C: 0.13, H: 25 })
    expect(parseOklch('#E53935')).toBeNull()
    expect(parseOklch('var(--x)')).toBeNull()
    expect(parseOklch('')).toBeNull()
  })
})

describe('oklchToRgb 端点正确', () => {
  it('L=1 C=0 → 白; L=0 C=0 → 黑', () => {
    expect(oklchToRgb(1, 0, 0)).toEqual({ r: 255, g: 255, b: 255 })
    expect(oklchToRgb(0, 0, 0)).toEqual({ r: 0, g: 0, b: 0 })
  })

  it('色相决定色系: H=25 偏红, H=155 偏绿', () => {
    const red = oklchToRgb(0.62, 0.15, 25)
    const green = oklchToRgb(0.62, 0.15, 155)
    expect(red.r).toBeGreaterThan(red.g)
    expect(green.g).toBeGreaterThan(green.r)
  })

  it('越界色度被裁剪, 绝不产出 NaN/负数/>255', () => {
    for (const [L, C, H] of [
      [1.5, 0.4, 25],
      [-0.5, 0.4, 25],
      [0.7, 0.9, 300],
    ] as const) {
      const c = oklchToRgb(L, C, H)
      for (const v of [c.r, c.g, c.b]) {
        expect(Number.isFinite(v)).toBe(true)
        expect(v).toBeGreaterThanOrEqual(0)
        expect(v).toBeLessThanOrEqual(255)
      }
    }
  })
})

describe('oklchStringToHex / rgbToHex', () => {
  it('字符串直转; 非 oklch 返回 null', () => {
    expect(oklchStringToHex('oklch(1 0 0)')).toBe('#ffffff')
    expect(oklchStringToHex('oklch(0 0 0)')).toBe('#000000')
    expect(oklchStringToHex('hsl(0 0% 0%)')).toBeNull()
  })

  it('hex 补零(单位数通道不会变成 1 位)', () => {
    expect(rgbToHex({ r: 1, g: 2, b: 3 })).toBe('#010203')
  })
})
