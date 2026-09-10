// P1-1 板块热力图(借鉴 OpenTerminal) 纯逻辑单测:
// 色阶映射(A股红涨绿跌, ±3% 夹紧) + treemap 数据变换(面积=成交额, 无数据保底可见)。
import { describe, expect, it } from 'vitest'
import {
  formatHeatPct,
  heatCellColor,
  heatLabelColor,
  toTreemapCells,
  type BoardHeatItem,
  type HeatPalette,
} from '@panwatch/biz-ui/lib/board-heatmap'

const PALETTE: HeatPalette = {
  up: '#E53935',
  down: '#43A047',
  neutral: 'rgba(100,116,139,0.18)',
  labelDark: '#1a1a1a',
  labelLight: '#ffffff',
}

const item = (over: Partial<BoardHeatItem>): BoardHeatItem => ({
  block_code: 'URFI0001',
  name: '半导体',
  board_type: 'industry',
  change_pct: 1.0,
  fund_net: null,
  volume: 1000,
  date: '2026-09-10',
  has_daily: true,
  ...over,
})

describe('heatCellColor 色阶映射', () => {
  it('无数据(null) → neutral 灰, 不编造涨跌色', () => {
    expect(heatCellColor(null, PALETTE)).toBe(PALETTE.neutral)
  })

  it('涨用 up 红, 跌用 down 绿', () => {
    expect(heatCellColor(1.0, PALETTE)).toContain('229, 57, 53')
    expect(heatCellColor(-1.0, PALETTE)).toContain('67, 160, 71')
  })

  it('幅度越大透明度越高, 且 ±3% 夹紧后封顶一致', () => {
    const a = heatCellColor(2.9, PALETTE)
    const b = heatCellColor(8.0, PALETTE)
    const c = heatCellColor(3.0, PALETTE)
    expect(b).toBe(c) // 夹紧
    const alpha = (s: string) => Number(s.split(',')[3]?.replace(')', ''))
    expect(alpha(c)).toBeGreaterThan(alpha(a))
    expect(alpha(a)).toBeGreaterThan(alpha(heatCellColor(0.2, PALETTE)))
  })

  it('0% 平盘 → neutral 灰(全站平盘灰惯例), 极小幅度同样归灰', () => {
    expect(heatCellColor(0, PALETTE)).toBe(PALETTE.neutral)
    expect(heatCellColor(0.002, PALETTE)).toBe(PALETTE.neutral)
  })
})

describe('heatLabelColor 对比度', () => {
  it('深色块(大涨幅) → 浅色字', () => {
    expect(heatLabelColor(3.0, PALETTE)).toBe(PALETTE.labelLight)
  })
  it('浅色块(小涨幅/平价) → 深色字', () => {
    expect(heatLabelColor(0.1, PALETTE)).toBe(PALETTE.labelDark)
    expect(heatLabelColor(0, PALETTE)).toBe(PALETTE.labelDark)
  })
  it('无数据 → 深色字(灰底)', () => {
    expect(heatLabelColor(null, PALETTE)).toBe(PALETTE.labelDark)
  })
})

describe('formatHeatPct 文案', () => {
  it('正数带 +, 保留两位; 无数据显式"无数据"', () => {
    expect(formatHeatPct(2.345)).toBe('+2.35%')
    expect(formatHeatPct(-1.2)).toBe('-1.20%')
    expect(formatHeatPct(null)).toBe('无数据')
  })
})

describe('toTreemapCells treemap 数据变换', () => {
  it('areaMetric=volume: 面积取成交额, 逐项映射字段', () => {
    const cells = toTreemapCells([item({ volume: 5000 })], { palette: PALETTE, areaMetric: 'volume' })
    expect(cells).toHaveLength(1)
    expect(cells[0].name).toBe('半导体')
    expect(cells[0].value).toBe(5000)
    expect(cells[0].blockCode).toBe('URFI0001')
    expect(cells[0].changePct).toBe(1.0)
  })

  it('volume 缺失/为 0 的板块 → 保底面积(中位数 2%), 不因 0 面积消失', () => {
    const cells = toTreemapCells(
      [
        item({ block_code: 'A', volume: 1_000_000 }),
        item({ block_code: 'B', volume: 3_000_000 }),
        item({ block_code: 'C', volume: null, has_daily: false, change_pct: null, date: null }),
      ],
      { palette: PALETTE, areaMetric: 'volume' },
    )
    const c = cells.find((x) => x.blockCode === 'C')!
    expect(c.value).toBeGreaterThan(0)
    expect(c.value).toBeLessThan(cells.find((x) => x.blockCode === 'B')!.value)
  })

  it('areaMetric=equal: 等权面积, 全部一致', () => {
    const cells = toTreemapCells(
      [item({ block_code: 'A', volume: 1 }), item({ block_code: 'B', volume: 999999 })],
      { palette: PALETTE, areaMetric: 'equal' },
    )
    expect(cells[0].value).toBe(cells[1].value)
  })

  it('色与标签色逐项写入 itemStyle/label; 无数据项走 neutral', () => {
    const cells = toTreemapCells(
      [
        item({ block_code: 'A', change_pct: 3.0 }),
        item({ block_code: 'B', change_pct: null, has_daily: false }),
      ],
      { palette: PALETTE, areaMetric: 'volume' },
    )
    expect(cells[0].itemStyle.color).toBe(heatCellColor(3.0, PALETTE))
    expect(cells[0].label.color).toBe(PALETTE.labelLight)
    expect(cells[1].itemStyle.color).toBe(PALETTE.neutral)
    expect(cells[1].label.color).toBe(PALETTE.labelDark)
  })

  it('空列表 → 空数组(不抛)', () => {
    expect(toTreemapCells([], { palette: PALETTE, areaMetric: 'volume' })).toEqual([])
  })
})
