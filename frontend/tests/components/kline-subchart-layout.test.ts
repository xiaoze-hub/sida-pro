/**
 * 主图/副图不重合的钉子（2026-09-19 用户报"K 线图和下面的成交量指标重合"）。
 *
 * 轻量图表的副图（成交量/MACD/活跃度/资金柱）是**同 pane 的 overlay**，靠 priceScale 的
 * `scaleMargins` 挤到下方；主图价格轴必须把这块空间**让出来**，否则两者会画在同一片像素上。
 *
 * 历史 bug：主图价格轴没设 scaleMargins（默认 top .2 / bottom .1）→ K 线最低画到 90% 高度，
 * 而副图从 70% 开始 → **20% 高度重叠带**。
 *
 * 判据（源码级，防回归）：主图价格轴 bottom ≥ 副图 overlay top。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const raw = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'), 'utf-8')
const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

function num(name: string): number {
  const m = src.match(new RegExp(`const ${name} = ([0-9.]+)`))
  expect(m, `找不到常量 ${name}`).toBeTruthy()
  return parseFloat((m as RegExpMatchArray)[1])
}

describe('K线主图 / 副图 不得重合', () => {
  it('主图价格轴底部让出的空间 ≥ 副图占用的顶部起点', () => {
    const subTop = num('SUBCHART_TOP')
    const gap = num('HAIRLINE_GAP')
    // 主图 bottom 在源码里写成 `1 - SUBCHART_TOP + HAIRLINE_GAP`，这里按同样公式算
    expect(src).toMatch(/const PRICE_SCALE_MARGINS = \{ top: [0-9.]+, bottom: 1 - SUBCHART_TOP \+ HAIRLINE_GAP \}/)
    const priceBottom = 1 - subTop + gap
    // 不变量: 主图**底边距** ≥ 副图**顶部起点**到 pane 底部的距离。
    // 价格轴 margins.bottom=B ⇒ 价格区画到 (1-B); 副图从 S 开始 ⇒ 不重合要求 (1-B) ≤ S ⇒ B ≥ 1-S。
    // (第一版把 B 直接和 S 比 —— 分数语义不同, 比错了: 0.32 vs 0.7 明明是对的却判失败)
    expect(priceBottom).toBeGreaterThanOrEqual(1 - subTop)
    expect(gap).toBeGreaterThan(0) // 留一点间隙当分隔线，别贴着
  })

  it('主图价格轴确实应用了这份 margins（设了常量但没 apply = 白设）', () => {
    expect(src).toMatch(/series\.priceScale\(\)\.applyOptions\(\{ scaleMargins: PRICE_SCALE_MARGINS \}\)/)
  })

  it('副图 overlay（成交量/资金柱）用同一个常量，不许各写各的数字', () => {
    const uses = src.match(/scaleMargins: SUBCHART_MARGINS/g) || []
    expect(uses.length).toBeGreaterThanOrEqual(2) // 成交量 + 资金柱
    // 不许再出现写死的 0.7（否则两处又会漂移）
    expect(src).not.toMatch(/scaleMargins: \{ top: 0\.7, bottom: 0 \}/)
  })

  it('容器暴露布局钩子，生产巡检能验（画在 canvas 里的东西 DOM 量不到）', () => {
    expect(src).toContain('data-chart-layout')
    // 用 safeFixed(项目门禁 R6 要求), 不许裸 toFixed
    expect(src).toMatch(/safeFixed\(PRICE_SCALE_MARGINS\.bottom, 3\)\}\/\$\{safeFixed\(SUBCHART_MARGINS\.top, 3\)/)
  })
})
