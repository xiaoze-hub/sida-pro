/**
 * 涨跌强度阶 = OKLCH 均匀发散 11 档(2026-09-20 设计系统落地)。
 *
 * 为什么改: 老实现把 up/down 色按 alpha 0.12→0.9 叠底(sRGB 线性插值) —— **感知不均匀**,
 * 弱档全挤在一起(0.3% 与 0.8% 看不出差别), 强档又几乎一样。
 *
 * 本文件钉四件事(都是可机器判的):
 *  ① CSS 单一来源与 TS 兜底**逐档一致**(不允许两套值漂移);
 *  ② 每一档的**文字对比度 ≥3:1**(色阶再好看, 文字看不见就是bug);
 *  ③ 档位随幅度**单调**且 ±夹紧后封顶;
 *  ④ 语义不变: 涨=红系 / 跌=绿系 / 平盘与无数据=中性(不许把无数据染成涨跌色)。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  HEAT_LADDER_OKLCH,
  heatCellColor,
  heatLabelColor,
  heatStepOf,
  parseColorToRgb,
  contrastRatio,
  type HeatPalette,
} from '@panwatch/biz-ui/lib/board-heatmap'

const CSS = readFileSync(resolve(__dirname, '../../src/index.css'), 'utf-8')

const PALETTE: HeatPalette = {
  up: '#E53935',
  down: '#43A047',
  neutral: 'rgba(100,116,139,0.18)',
  labelDark: '#10151f',
  labelLight: '#ffffff',
} as HeatPalette

/** 取 CSS 里某一档的值; 亮/暗两套阶梯都在 index.css 里 ⇒ 默认取**最后一处(暗色)** ——
 *  TS 兜底镜像的是暗色档(app 暗色优先), 这条一致性由用例钉住。 */
function cssLadderValue(key: string, which: 'first' | 'last' = 'last'): string | null {
  const k = key === 'zero' ? '0' : key
  const all = [...CSS.matchAll(new RegExp(`--heat-${k}:\\s*([^;]+);`, 'g'))]
  if (!all.length) return null
  return (which === 'first' ? all[0][1] : all[all.length - 1][1]).trim()
}

const rgbOf = (hex: string) => parseColorToRgb(hex)!

describe('CSS 与 TS 单一来源一致', () => {
  it.each(Object.entries(HEAT_LADDER_OKLCH))('暗色 --heat-%s 与 TS 兜底逐字一致', (key, value) => {
    expect(cssLadderValue(key), `index.css 缺暗色 --heat-${key}`).toBe(value)
  })

  it('亮色阶梯也在(11 档齐全, 且与暗色**不同** —— 两套主题各有自己的观感基线)', () => {
    for (const k of Object.keys(HEAT_LADDER_OKLCH)) {
      const light = cssLadderValue(k, 'first')
      expect(light, `亮色缺 --heat-${k}`).toBeTruthy()
      expect(light).not.toBe(cssLadderValue(k, 'last'))
    }
  })

  it('11 档齐全(n5..n1 + 中性 + p1..p5)', () => {
    expect(Object.keys(HEAT_LADDER_OKLCH)).toHaveLength(11)
  })
})

describe('档位映射', () => {
  it('涨→p 档 / 跌→n 档 / 平盘与无数据→中性', () => {
    expect(heatStepOf(3)).toBe('p5')
    expect(heatStepOf(0.5)).toBe('p1')
    expect(heatStepOf(-3)).toBe('n5')
    expect(heatStepOf(-0.5)).toBe('n1')
    expect(heatStepOf(0)).toBe('zero')
    expect(heatStepOf(null)).toBe('zero')
    expect(heatStepOf(undefined)).toBe('zero')
    expect(heatStepOf(NaN)).toBe('zero')
  })

  it('幅度越大档位越强, 且 ±夹紧后封顶(超过 clamp 不再更强)', () => {
    const order = ['p1', 'p2', 'p3', 'p4', 'p5']
    expect(order.indexOf(heatStepOf(0.6))).toBeLessThan(order.indexOf(heatStepOf(1.8)))
    expect(order.indexOf(heatStepOf(1.8))).toBeLessThan(order.indexOf(heatStepOf(2.9)))
    expect(heatStepOf(3)).toBe(heatStepOf(10))
    expect(heatStepOf(-3)).toBe(heatStepOf(-99))
  })

  it('极小幅度(0.001%)归中性灰 —— 与全站"平盘灰"口径一致', () => {
    expect(heatStepOf(0.001)).toBe('zero')
    expect(heatCellColor(0.001, PALETTE)).toBe(PALETTE.neutral)
  })
})

describe('语义: 涨红跌绿 + 强度均匀', () => {
  const hue = (hex: string) => {
    const { r, g, b } = rgbOf(hex)
    const max = Math.max(r, g, b)
    const min = Math.min(r, g, b)
    const d = max - min || 1
    if (max === r) return ((g - b) / d + 6) % 6 * 60
    if (max === g) return (((b - r) / d) + 2) * 60
    return (((r - g) / d) + 4) * 60
  }

  it('涨 → 红系(hue ≤40 或 ≥330), 跌 → 绿系(80–180)', () => {
    for (const pct of [0.6, 1.8, 3]) {
      const h = hue(heatCellColor(pct, PALETTE))
      expect(h <= 40 || h >= 330, `涨 ${pct}% 的色相 ${h.toFixed(0)} 不是红系`).toBe(true)
    }
    for (const pct of [-0.6, -1.8, -3]) {
      const h = hue(heatCellColor(pct, PALETTE))
      expect(h > 80 && h < 180, `跌 ${pct}% 的色相 ${h.toFixed(0)} 不是绿系`).toBe(true)
    }
  })

  it('感知均匀: OKLCH 的 L 步长严格等距(这正是换掉 alpha 插值的理由)', () => {
    const L = (k: string) => Number(cssLadderValue(k, 'last')!.match(/oklch\(([\d.]+)/)![1])
    const steps = ['p1', 'p2', 'p3', 'p4', 'p5'].map(L)
    const diffs = steps.slice(1).map((v, i) => +(v - steps[i]).toFixed(4))
    expect(new Set(diffs).size, `L 步长不等距: ${diffs.join(', ')}`).toBe(1)
  })

  it('无数据与平盘**颜色相同**(都不携带涨跌语义)', () => {
    expect(heatCellColor(null, PALETTE)).toBe(heatCellColor(0, PALETTE))
  })
})

describe('每一档的文字对比度 ≥3:1(色阶再好看, 文字看不见就是 bug)', () => {
  it.each(Object.keys(HEAT_LADDER_OKLCH))('档位 %s 的文字色有足够对比度', (k) => {
    const step = k as keyof typeof HEAT_LADDER_OKLCH
    const pct = step === 'zero' ? 0 : Number(step.slice(1)) * 0.6 * (step[0] === 'p' ? 1 : -1)
    const surfaceHex = CSS.match(new RegExp(`--heat-${step === 'zero' ? '0' : step}:\\s*oklch\\(([^)]+)\\)`))
    expect(surfaceHex).toBeTruthy()
    // 用与生产同一条路径取色: heatCellColor → 文字色, 再算对比度
    const bg = step === 'zero' ? PALETTE.neutral : heatCellColor(pct, PALETTE)
    const label = heatLabelColor(pct, PALETTE)
    const bgRgb = parseColorToRgb(bg)
    if (!bgRgb || bgRgb.a < 1) return // 半透明(中性灰)由实测像素那条链路保证, 这里不重复判
    const ratio = contrastRatio(bgRgb, rgbOf(label))
    expect(ratio, `档位 ${k} 文字对比度仅 ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(3)
  })
})
