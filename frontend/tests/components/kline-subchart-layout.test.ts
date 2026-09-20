/**
 * K线副图 = **独立 pane**（2026-09-20 架构改动，承接用户报的"K线与成交量重合"）。
 *
 * 演进：
 *   ① 原来副图是**同 pane overlay**，靠 `scaleMargins` 挤到底部 → 主图没让位就画进同一片像素（用户报的重合）；
 *   ② 09-19 的修法是"主图价格轴让出底部 30%" —— 能用，但**靠两个数字对齐**，改一处就复发；
 *   ③ 现在改成**真 pane**（paneIndex=1）：主图/副图在不同画布上，结构上不可能重合，
 *      副图还顺带拿到自己的坐标轴。
 *
 * 本地实测（dev server 接生产数据，个股页）：`data-chart-panes = "2|290/101"` →
 * 主图 290px / 副图 101px（约 26%），副图 canvas 确有绘制（墨迹比 0.26，坐标轴 0.27）。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const raw = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'), 'utf-8')
const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

describe('副图必须是独立 pane', () => {
  it('副图系列挂到 paneIndex=1（成交量 / 资金柱 / MACD 都要）', () => {
    expect(src).toMatch(/const SUBCHART_PANE = 1/)
    const withPane = src.match(/SUBCHART_PANE,\s*\)/g) || []
    expect(withPane.length).toBeGreaterThanOrEqual(3)
  })

  it('主图价格轴不再给副图让位（旧的 0.32 是"让位"留下的疤）', () => {
    const m = src.match(/const PRICE_SCALE_MARGINS = \{ top: ([0-9.]+), bottom: ([0-9.]+) \}/)
    expect(m, 'PRICE_SCALE_MARGINS 结构变了 —— 判据要跟着改，别静默失效').toBeTruthy()
    expect(parseFloat((m as RegExpMatchArray)[2])).toBeLessThan(0.15)
  })

  it('旧的 overlay 常量不许复活（否则两套布局逻辑并存）', () => {
    expect(src).not.toContain('SUBCHART_MARGINS')
    expect(src).not.toContain('SUBCHART_TOP')
    expect(src).not.toContain('HAIRLINE_GAP')
  })

  it('副图 pane 占比设置存在（否则副图会塌成一条缝）', () => {
    expect(src).toMatch(/chart\.panes\(\)\[SUBCHART_PANE\]\?\.setStretchFactor\(SUBCHART_STRETCH\)/)
  })

  it('验收钩子挂的是**运行时** pane 数+高度，不是配置常量', () => {
    // 配置说"分了 pane"不等于运行时真分了（旧内核会静默退化）
    expect(src).toContain('data-chart-panes')
    expect(src).toMatch(/chart\.panes\(\)/)
    expect(src).toMatch(/getHeight\(\)/)
  })
})
