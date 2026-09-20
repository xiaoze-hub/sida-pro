// 热力图标签: 从"按面积占比隐藏"改成"按**真实像素尺寸**定档位"(2026-09-20 修缺陷)。
//
// 用户报的现象: "板块名和涨跌百分比有的不显示, 切到面积等权**都不显示**, 鼠标放上去才显示"。
// 根因: 老规则用 `面积占比 ≥ 0.8%` 当"放不放得下文字"的代理 —— 等权模式下每块占比 = 1/N,
// N=128 时恒为 0.0078 < 0.008 ⇒ **整张图一个标签都没有**; 量能模式下也只有少数大块够阈值。
//
// 新架构: 占比不再参与判断(占比不是像素尺寸)。第一遍全显示, 布局算好后由 BoardHeatmap 的
// **二遍**按 ECharts 实测 rect 调 `labelTierFor` 降档 —— 档位是纯函数, 在这里钉住。
import { describe, expect, it } from 'vitest'
import {
  LABEL_MIN_H_1LINE,
  LABEL_MIN_H_2LINE,
  LABEL_MIN_W_1LINE,
  LABEL_MIN_W_2LINE,
  labelTierFor,
  toTreemapCells,
  type BoardHeatItem,
  type HeatPalette,
} from '@panwatch/biz-ui/lib/board-heatmap'

const PALETTE: HeatPalette = {
  up: '#E53935',
  down: '#43A047',
  neutral: 'rgba(100,116,139,0.18)',
  labelDark: '#111111',
  labelLight: '#ffffff',
} as HeatPalette

const item = (over: Partial<BoardHeatItem>): BoardHeatItem =>
  ({
    name: over.name ?? '板块',
    block_code: over.block_code ?? 'BK0001',
    change_pct: over.change_pct ?? 1,
    volume: over.volume ?? 1e9,
    fund_net: null,
    date: '2026-09-20',
    has_daily: true,
    volume_ratio: null,
    speed: null,
    ...over,
  }) as BoardHeatItem

describe('标签档位按真实像素尺寸定(不再按面积占比)', () => {
  it('装得下两行 → 档位 2; 只装得下一行 → 档位 1; 都装不下 → 档位 0', () => {
    expect(labelTierFor(LABEL_MIN_W_2LINE, LABEL_MIN_H_2LINE)).toBe(2)
    expect(labelTierFor(200, 90)).toBe(2)
    expect(labelTierFor(LABEL_MIN_W_1LINE, LABEL_MIN_H_1LINE)).toBe(1)
    expect(labelTierFor(40, 20)).toBe(1)
    expect(labelTierFor(10, 6)).toBe(0)
    expect(labelTierFor(4, 2)).toBe(0)
  })

  it('尺寸读不到(NaN/Infinity) → 按能显示处理(宁可多显示, 不许静默丢标签)', () => {
    expect(labelTierFor(NaN, 50)).toBe(2)
    expect(labelTierFor(50, Infinity)).toBe(2)
  })

  it('两行阈值严于一行阈值(档位单调, 不会出现"能显示一行却判 0")', () => {
    expect(LABEL_MIN_W_2LINE).toBeGreaterThan(LABEL_MIN_W_1LINE)
    expect(LABEL_MIN_H_2LINE).toBeGreaterThan(LABEL_MIN_H_1LINE)
    for (let w = 0; w <= 60; w += 4) {
      for (let h = 0; h <= 40; h += 4) {
        const t = labelTierFor(w, h)
        if (w >= LABEL_MIN_W_2LINE && h >= LABEL_MIN_H_2LINE) expect(t).toBe(2)
        else if (w >= LABEL_MIN_W_1LINE && h >= LABEL_MIN_H_1LINE) expect(t).toBe(1)
        else expect(t).toBe(0)
      }
    }
  })
})

describe('等权模式的回归(用户报的"都不显示")', () => {
  it('等权 128 块: 每块占比 0.78%, 低于老阈值 0.8% —— 老规则必然全隐(记录根因)', () => {
    const items = Array.from({ length: 128 }, (_, i) =>
      item({ name: `板块${i}`, block_code: `BK${i}`, volume: 1e9 }),
    )
    const cells = toTreemapCells(items, { palette: PALETTE, areaMetric: 'equal' })
    const total = cells.reduce((a, c) => a + c.value, 0)
    const share = cells[0].value / total
    expect(share).toBeCloseTo(1 / 128, 6)
    expect(share).toBeLessThan(0.008) // ← 老阈值: 这就是"切到等权一个标签都没有"的原因
  })

  it('toTreemapCells 第一遍**不再**按占比隐藏任何块(全显示, 由二遍按尺寸降档)', () => {
    const items = Array.from({ length: 128 }, (_, i) =>
      item({ name: `板块${i}`, block_code: `BK${i}`, volume: i === 0 ? 1e12 : 1e6 }),
    )
    const cells = toTreemapCells(items, { palette: PALETTE, areaMetric: 'equal' })
    expect(cells.every((c) => c.label.show !== false)).toBe(true)
    expect(cells.every((c) => c.labelTier === 2)).toBe(true)
  })

  it('量能模式: 大小块一视同仁(老规则会隐掉小块 —— 这就是"有的不显示")', () => {
    const items = [
      item({ name: '大', block_code: 'BK1', volume: 1e12 }),
      item({ name: '小', block_code: 'BK2', volume: 1e6 }),
    ]
    const cells = toTreemapCells(items, { palette: PALETTE, areaMetric: 'volume' })
    expect(cells.every((c) => c.label.show !== false)).toBe(true)
  })
})

describe('口径不变: 面积仍是量能/等权原值', () => {
  it('面积口径不因标签逻辑变化', () => {
    const cells = toTreemapCells(
      [item({ name: 'A', volume: 1234 }), item({ name: 'B', volume: 5678 })],
      { palette: PALETTE, areaMetric: 'volume' },
    )
    expect(cells.find((c) => c.name === 'A')!.value).toBe(1234)
    expect(cells.find((c) => c.name === 'B')!.value).toBe(5678)
  })

  it('无当日数据的保底块: 面积仍 > 0(不会 0 面积整块消失)', () => {
    const cells = toTreemapCells(
      [item({ name: '有量', volume: 1e9 }), item({ name: '无量', volume: null, has_daily: false })],
      { palette: PALETTE, areaMetric: 'volume' },
    )
    expect(cells.find((c) => c.name === '无量')!.value).toBeGreaterThan(0)
  })
})
