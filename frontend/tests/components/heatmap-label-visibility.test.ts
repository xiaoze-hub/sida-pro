/**
 * 热力图标签可见性(2026-09-20 修缺陷)。
 *
 * 用户报: "板块名称和涨跌百分比有的不显示, 切换到面积等权**都不显示**, 鼠标放上去才显示"。
 * 两个根因(都在这里钉住):
 *   ① 显不显示由**面积占比**决定 —— 等权模式下每块占比 = 1/N, N=128 时恒 0.0078 < 老阈值
 *      0.008 ⇒ 整张图无标签。真判据只能是**布局后的真实像素尺寸**。
 *   ② 字色由 **alpha 代理** + 主题名决定 —— 深色主题里 `--foreground` 是近白, 两个候选都是
 *      浅色 ⇒ 浅色块上的文字隐形。真判据只能是**实测底色亮度**。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const raw = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/components/dashboard/BoardHeatmap.tsx'), 'utf-8')
const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

describe('热力图标签: 显不显示按真实尺寸', () => {
  it('第一遍 setOption 之后跑二遍布局(布局前不知道每块多大)', () => {
    expect(src).toMatch(/applyLabelTiers\(chart, cells/)
    const first = src.indexOf('true,\n    )')
    const second = src.indexOf('applyLabelTiers(chart, cells')
    expect(second).toBeGreaterThan(first)
  })

  it('布局从 treemap **树节点**读(treemap 的 rect 不在 List 上)', () => {
    expect(src).toMatch(/tree\?\.root\?\.children/)
    expect(src).toMatch(/getLayout\?\.\(\)/)
  })

  it('叶子数与色块数不等 → 宁可不改(顺序错位会把标签配到别的板块上)', () => {
    expect(src).toMatch(/leaves\.length !== rects\.length/)
  })

  it('尺寸档位由纯函数定, 且 0 档渲染空串(只留 tooltip)', () => {
    expect(src).toMatch(/labelTierFor\(r\.w, r\.h\)/)
    expect(src).toMatch(/if \(tier === 0\) return ''/)
    expect(src).toMatch(/if \(tier === 1\) return `\$\{p\?\.name \?\? ''\}`/)
  })
})

describe('热力图标签: 字色按实测底色', () => {
  it('从**画布实测像素**取底色(半透明填充的 RGB 不是眼睛看到的颜色)', () => {
    expect(src).toMatch(/function sampleTileColors/)
    expect(src).toMatch(/getImageData/)
    expect(src).toMatch(/pickLabelColor\(bg, LABEL_DARK, LABEL_LIGHT\)/)
  })

  it('采样点避开文字(上缘内缩), 否则会把字色当底色', () => {
    expect(src).toMatch(/h \* 0\.12/)
  })

  it('字色候选是真正的深浅两端 —— 不许再取 --foreground(深色主题里它是近白)', () => {
    expect(src).toMatch(/const LABEL_DARK = '#10151f'/)
    expect(src).toMatch(/const LABEL_LIGHT = '#ffffff'/)
    expect(src).not.toMatch(/labelDark: hslaVar\('--foreground'/)
  })

  it('读不到像素不硬改(保留第一遍估算, 并如实标注 unknown)', () => {
    expect(src).toMatch(/data-heatmap-contrast/)
    expect(src).toMatch(/'unknown'/)
  })
})

describe('验收钩子(画布里的文字 DOM 量不到, 必须挂出来)', () => {
  it('挂 显示/总数 · 太小数 · 最小块 · 最小对比度', () => {
    expect(src).toContain('data-heatmap-labels')
    expect(src).toContain('data-heatmap-label-tiny')
    expect(src).toContain('data-heatmap-min-tile')
    expect(src).toContain('data-heatmap-contrast')
  })
})
