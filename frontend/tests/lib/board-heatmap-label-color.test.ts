// 热力图字色必须按**实测底色亮度**选(2026-09-20 修缺陷)。
//
// 用户报: "板块名和涨跌百分比有的不显示, 鼠标放上去才显示"。
// 根因②: 老规则 `alphaOf(r) > 0.45 ? 白字 : labelDark` 有两个致命假设 ——
//   ① 拿 **alpha 当亮度代理**(填充色偏亮时 alpha 大 ≠ 底色深, 白字压上去等于看不见);
//   ② 假设 labelDark 一定是深色 —— 深色主题里 `--foreground` 是**近白**, 两个候选都是浅色
//      ⇒ 浅色块上的文字必然隐形。
// 实测: 这块画布底色是**白**的, 色块是半透明填充叠出来的浅色调 ⇒ 必须用深字。
import { describe, expect, it } from 'vitest'
import {
  LABEL_LUMINANCE_THRESHOLD,
  compositeOver,
  contrastRatio,
  heatCellColor,
  heatLabelColor,
  parseColorToRgb,
  pickLabelColor,
  relativeLuminance,
  type HeatPalette,
} from '@panwatch/biz-ui/lib/board-heatmap'

const PALETTE: HeatPalette = {
  up: '#E53935',
  down: '#43A047',
  neutral: 'rgba(100,116,139,0.18)',
  labelDark: '#10151f',
  labelLight: '#ffffff',
  surface: '#ffffff',
} as HeatPalette

const rgb = (r: number, g: number, b: number) => ({ r, g, b, a: 1 })

describe('颜色工具', () => {
  it('相对亮度: 黑 0 / 白 1; 对比度黑白 = 21', () => {
    expect(relativeLuminance(rgb(0, 0, 0))).toBeCloseTo(0, 5)
    expect(relativeLuminance(rgb(255, 255, 255))).toBeCloseTo(1, 5)
    expect(contrastRatio(rgb(255, 255, 255), rgb(0, 0, 0))).toBeCloseTo(21, 1)
  })

  it('解析 #hex / rgb() / rgba() / hsl() / hsla(), 解析不了返回 null(不猜)', () => {
    expect(parseColorToRgb('#fff')).toEqual({ r: 255, g: 255, b: 255, a: 1 })
    expect(parseColorToRgb('#E53935')).toEqual({ r: 229, g: 57, b: 53, a: 1 })
    expect(parseColorToRgb('rgba(229, 57, 53, 0.9)')!.a).toBeCloseTo(0.9, 3)
    const hsl = parseColorToRgb('hsl(0, 100%, 50%)')!
    expect(hsl.r).toBe(255)
    expect(hsl.g).toBe(0)
    expect(parseColorToRgb('hsla(215, 16%, 65%, 0.18)')!.a).toBeCloseTo(0.18, 3)
    expect(parseColorToRgb('var(--x)')).toBeNull()
  })

  it('半透明叠底: 0.9 alpha 的红叠白底 ≈ 231,77,73(实测生产就是这个值)', () => {
    const shown = compositeOver(parseColorToRgb('rgba(229,57,53,0.9)')!, rgb(255, 255, 255))
    expect(Math.abs(shown.r - 231)).toBeLessThanOrEqual(2)
    expect(Math.abs(shown.g - 77)).toBeLessThanOrEqual(3)
    expect(Math.abs(shown.b - 73)).toBeLessThanOrEqual(3)
  })
})

describe('字色按实测底色亮度选(不再按 alpha 猜)', () => {
  it('浅色块(实测 247,196,195) → 深字; 这就是修复前"看不见"的那批块', () => {
    const picked = pickLabelColor(rgb(247, 196, 195), '#10151f', '#ffffff')
    expect(picked.color).toBe('#10151f')
    expect(picked.contrast).toBeGreaterThan(4.5)
  })

  it('深色块(暗红 90,30,28) → 白字', () => {
    const picked = pickLabelColor(rgb(90, 30, 28), '#10151f', '#ffffff')
    expect(picked.color).toBe('#ffffff')
    expect(picked.contrast).toBeGreaterThan(4.5)
  })

  it('阈值两端都保底 3:1(字色切换点不会出现"两边都看不清")', () => {
    // 阈值上方的极端(刚过线)与下方的极端(刚好在线)都不得低于 3:1
    const just = LABEL_LUMINANCE_THRESHOLD
    const below = rgb(Math.round(just * 200), Math.round(just * 180), Math.round(just * 170))
    const above = rgb(Math.round(just * 255), Math.round(just * 250), Math.round(just * 245))
    expect(pickLabelColor(below, '#10151f', '#ffffff').contrast).toBeGreaterThanOrEqual(2.9)
    expect(pickLabelColor(above, '#10151f', '#ffffff').contrast).toBeGreaterThanOrEqual(4.0)
  })
})

describe('heatLabelColor 第一遍估算(饱和红→白字, 浅色块→深字)', () => {
  it('强涨(饱和红) → 白字', () => {
    expect(heatLabelColor(3, PALETTE, 3)).toBe('#ffffff')
  })

  it('微涨(浅色块) → 深字 —— 修复前这里是白字, 等于隐形', () => {
    expect(heatLabelColor(0.2, PALETTE, 3)).toBe('#10151f')
  })

  it('无数据(灰底) → 深字', () => {
    expect(heatLabelColor(null, PALETTE, 3)).toBe('#10151f')
  })

  it('不再依赖 alpha 代理: **同一个填充色**, 叠白底/深底上字色必须相反', () => {
    // 微涨的填充(低 alpha)叠白底 = 浅粉 → 深字; 叠深底 = 暗红 → 白字。
    // 老规则只看 alpha(同一个值), 会给出**同一个**字色 —— 这正是它错的地方。
    const fill = parseColorToRgb(heatCellColor(0.15, PALETTE, 3))!
    expect(fill.a).toBeLessThan(0.3)
    const onWhite = pickLabelColor(compositeOver(fill, rgb(255, 255, 255)), '#10151f', '#ffffff')
    const onDark = pickLabelColor(compositeOver(fill, rgb(10, 10, 15)), '#10151f', '#ffffff')
    expect(onWhite.color).toBe('#10151f')
    expect(onDark.color).toBe('#ffffff')
  })
})
