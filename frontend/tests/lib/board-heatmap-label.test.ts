// UI 走查 B3(2026-09-18): 热力图"小块文字挤成省略号"的回归。
// 只改**文字显不显示**, 不改面积口径(面积仍 = 量能), 这条断言一并钉住。
import { describe, expect, it } from 'vitest'
import {
  MIN_LABEL_SHARE,
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
    date: '2026-09-18',
    has_daily: true,
    volume_ratio: null,
    speed: null,
    ...over,
  }) as BoardHeatItem

describe('treemap 标签降级(小块不画文字)', () => {
  it('大块显示文字, 占比不足的极小快隐藏文字', () => {
    const items = [
      item({ name: '半导体', block_code: 'BK1', volume: 1e10 }),
      item({ name: '小板块A', block_code: 'BK2', volume: 1e6 }),
      item({ name: '小板块B', block_code: 'BK3', volume: 1e6 }),
    ]
    const cells = toTreemapCells(items, { palette: PALETTE, areaMetric: 'volume' })
    const big = cells.find((c) => c.name === '半导体')!
    const small = cells.find((c) => c.name === '小板块A')!
    expect(big.label.show).toBe(true)
    expect(small.label.show).toBe(false)
  })

  it('阈值与 MIN_LABEL_SHARE 一致(边界可预测)', () => {
    // 两块各 50% → 都远大于阈值 → 都显示
    const even = toTreemapCells(
      [item({ name: 'A', volume: 1000 }), item({ name: 'B', volume: 1000 })],
      { palette: PALETTE, areaMetric: 'volume' },
    )
    expect(even.every((c) => c.label.show)).toBe(true)
    // 一块 99.5% + 一块 0.5% → 小块低于阈值 → 隐藏
    const skewed = toTreemapCells(
      [item({ name: '大', volume: 199 }), item({ name: '小', volume: 1 })],
      { palette: PALETTE, areaMetric: 'volume' },
    )
    expect(skewed.find((c) => c.name === '大')!.label.show).toBe(true)
    expect(skewed.find((c) => c.name === '小')!.label.show).toBe(false)
    expect(MIN_LABEL_SHARE).toBeGreaterThan(0)
    expect(MIN_LABEL_SHARE).toBeLessThan(0.05)
  })

  it('口径不变: 面积仍是量能原值, 压缩只发生在"文字显不显示"这一层', () => {
    const cells = toTreemapCells(
      [item({ name: 'A', volume: 1234 }), item({ name: 'B', volume: 5678 })],
      { palette: PALETTE, areaMetric: 'volume' },
    )
    expect(cells.find((c) => c.name === 'A')!.value).toBe(1234)
    expect(cells.find((c) => c.name === 'B')!.value).toBe(5678)
  })

  it('无当日数据的保底块: 面积仍可见(>0), 标签该不该显示由占比决定', () => {
    const cells = toTreemapCells(
      [
        item({ name: '有量', volume: 1e9 }),
        item({ name: '无量', volume: null, has_daily: false }),
      ],
      { palette: PALETTE, areaMetric: 'volume' },
    )
    const stale = cells.find((c) => c.name === '无量')!
    expect(stale.value).toBeGreaterThan(0)
    expect(typeof stale.label.show).toBe('boolean')
  })
})
