/**
 * 1-A 全局 token 钉子（2026-10-24）
 *
 * 审计 G4 的验收线是「卡片边界对卡底 ≥1.5:1」，勘误 7.2 把原方案 `#2b2b3f`(1.31:1) 否掉，
 * 改成 `#3a3a52`(1.65:1)。这里直接**从 index.css 解析出真实 token 值**再算对比度 ——
 * 改错颜色会立刻变红，而不是等人眼发现"卡片没边界"。
 */
import { readFileSync } from 'fs'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

const CSS = readFileSync(join(__dirname, '../../src/index.css'), 'utf8')

/** 取第一个匹配的 CSS 变量值（dark 块在前，够用） */
function token(name: string): string {
  const m = CSS.match(new RegExp(`${name}:\\s*([^;]+);`))
  if (!m) throw new Error(`token ${name} not found`)
  return m[1].trim()
}

/** hsl(H S% L%) → 相对亮度 */
function hslLum(triple: string): number {
  const m = triple.match(/(-?[\d.]+)\s+([\d.]+)%\s+([\d.]+)%/)
  if (!m) throw new Error(`bad hsl triple: ${triple}`)
  const [h, s, l] = [Number(m[1]), Number(m[2]) / 100, Number(m[3]) / 100]
  const c = (1 - Math.abs(2 * l - 1)) * s
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
  const m0 = l - c / 2
  const seg = Math.floor(((h % 360) + 360) % 360 / 60)
  const rgb = [
    [c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x],
  ][seg].map((v) => v + m0)
  const lin = rgb.map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)))
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
}

const ratio = (a: number, b: number) => {
  const [hi, lo] = a > b ? [a, b] : [b, a]
  return (hi + 0.05) / (lo + 0.05)
}

describe('G4 卡片边界可见', () => {
  it('--border-strong 对卡片底 --s1 的对比度 ≥ 1.5:1', () => {
    const r = ratio(hslLum(token('--border-strong')), hslLum(token('--s1')))
    expect(r).toBeGreaterThanOrEqual(1.5)
    // 实测应约 1.65（#3a3a52 vs #15151f）
    expect(r).toBeLessThan(2.2)
  })

  it('卡片底 --s1 与页底 --s0 拉开至少一档（s1 > s0）', () => {
    expect(hslLum(token('--s1'))).toBeGreaterThan(hslLum(token('--s0')))
  })

  it('.card 用 --border-strong，不再用低对比的 border-border/60', () => {
    expect(CSS).toMatch(/\.card \{[\s\S]{0,120}border-border-strong/)
    expect(CSS).not.toMatch(/\.card \{[\s\S]{0,120}border-border\/60/)
  })
})

describe('G3 涨红三处一致', () => {
  it('--stock-up 是 #ff5252（不再是 4.41:1 的 #E53935）', () => {
    expect(token('--stock-up').toLowerCase()).toBe('#ff5252')
  })

  it('--stock-up-hsl 与 Hex 同色（漏改会让闪烁闪旧红）', () => {
    const hsl = token('--stock-up-hsl')
    expect(hsl).toBe('0 100% 66.1%')
  })
})
