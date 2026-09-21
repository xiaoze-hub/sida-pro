// P1-1 板块热力图(借鉴 OpenTerminal) 纯逻辑单测:
// 色阶映射(A股红涨绿跌, ±3% 夹紧) + treemap 数据变换(面积=成交额, 无数据保底可见)。
import { describe, expect, it } from 'vitest'
import {
  detectHeatAnomaly,
  formatHeatPct,
  hasDrawableArea,
  hasUsableVolume,
  heatCellColor,
  heatLabelColor,
  heatStepOf,
  contrastRatio,
  parseColorToRgb,
  toTreemapCells,
  usableVolumeCount,
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

/** 色相粗分: 只用来判"语义没被换掉"(红系/绿系), 不钉具体数值。 */
function hueOf(hex: string): '红' | '绿' | '其他' {
  const c = parseColorToRgb(hex)
  if (!c) return '其他'
  const max = Math.max(c.r, c.g, c.b)
  const min = Math.min(c.r, c.g, c.b)
  const d = max - min || 1
  const h = max === c.r ? (((c.g - c.b) / d + 6) % 6) * 60 : max === c.g ? ((c.b - c.r) / d + 2) * 60 : ((c.r - c.g) / d + 4) * 60
  if (h <= 40 || h >= 330) return '红'
  if (h > 80 && h < 180) return '绿'
  return '其他'
}

/** 文字与底色的对比度(背景若半透明 → 视为不足, 返回 0 让断言红) */
function contrastOf(label: string, bg: string): number {
  const b = parseColorToRgb(bg)
  const l = parseColorToRgb(label)
  if (!b || !l || b.a < 1) return 0
  return contrastRatio(b, l)
}

describe('heatCellColor 色阶映射', () => {
  it('无数据(null) → neutral 灰, 不编造涨跌色', () => {
    expect(heatCellColor(null, PALETTE)).toBe(PALETTE.neutral)
  })

  // 2026-09-20: 色阶由"sRGB alpha 插值"改为"OKLCH 均匀发散 11 档" ⇒ 断言改为**判色相**
  // (涨必须落在红系 / 跌必须落在绿系)。判色相而不是判具体 RGB: 换色阶不该把语义也换掉。
  it('涨用红系, 跌用绿系(判色相)', () => {
    expect(hueOf(heatCellColor(1.0, PALETTE))).toMatch(/红/)
    expect(hueOf(heatCellColor(-1.0, PALETTE))).toMatch(/绿/)
  })

  it('幅度越大档位越强, 且 ±3% 夹紧后封顶一致', () => {
    const b = heatCellColor(8.0, PALETTE)
    const c = heatCellColor(3.0, PALETTE)
    expect(b).toBe(c) // 夹紧: 超过 3% 不再更强
    const step = (pct: number) => Number(heatStepOf(pct).slice(1))
    expect(step(3.0)).toBe(5)
    expect(step(3.0)).toBeGreaterThan(step(2.0))
    expect(step(2.0)).toBeGreaterThan(step(1.0))
    expect(step(1.0)).toBeGreaterThan(step(0.2))
  })

  it('0% 平盘 → neutral 灰(全站平盘灰惯例), 极小幅度同样归灰', () => {
    expect(heatCellColor(0, PALETTE)).toBe(PALETTE.neutral)
    expect(heatCellColor(0.002, PALETTE)).toBe(PALETTE.neutral)
  })
})

// 2026-09-14 缺陷修复: -100 假暴跌。口径是"只把空当无数据":
// 前端**不按量级猜哨兵**, 缺数据必须由后端返回 null。理由见 board-heatmap.ts heatRatio 注释。
describe('heatCellColor: 缺数据(空)才算无数据, 不猜哨兵量级', () => {
  it('null/undefined/NaN/Infinity → neutral 灰(只有"空"才是无数据)', () => {
    expect(heatCellColor(null, PALETTE)).toBe(PALETTE.neutral)
    expect(heatCellColor(undefined, PALETTE)).toBe(PALETTE.neutral)
    expect(heatCellColor(Number.NaN, PALETTE)).toBe(PALETTE.neutral)
    expect(heatCellColor(Number.POSITIVE_INFINITY, PALETTE)).toBe(PALETTE.neutral)
  })

  it('真实跌幅照旧染绿系: -10% 与 -100% 都不许变灰(按量级猜会把真跌染成灰)', () => {
    expect(hueOf(heatCellColor(-10, PALETTE))).toMatch(/绿/)
    // -100 在 A 股板块上基本不可能, 但"是不是哨兵"只有数据源知道 —— 前端不猜,
    // 只如实按读数上色; 缺数据由后端改成 null 后自然走 neutral。
    expect(hueOf(heatCellColor(-100, PALETTE))).toMatch(/绿/)
    expect(heatCellColor(-100, PALETTE)).not.toBe(PALETTE.neutral)
  })

  it('百分本文案同样只对"空"显式无数据', () => {
    expect(formatHeatPct(undefined)).toBe('无数据')
    expect(formatHeatPct(Number.NaN)).toBe('无数据')
    expect(formatHeatPct(-10)).toBe('-10.00%')
  })
})

describe('heatLabelColor 对比度', () => {
  // 2026-09-20: 字色不再由 alpha 代理决定, 改由**实测底色亮度**在两端口之间选 ⇒
  // 断言改为"每一档都有足够对比度"(具体取哪一端由对比度算出来, 不写死)。
  it('强涨档: 文字与底色对比度 ≥3:1', () => {
    expect(contrastOf(heatLabelColor(3.0, PALETTE), heatCellColor(3.0, PALETTE))).toBeGreaterThanOrEqual(3)
  })
  it('微涨/平价: 文字看得见(对比度 ≥3:1)', () => {
    expect(contrastOf(heatLabelColor(0.1, PALETTE), heatCellColor(0.1, PALETTE))).toBeGreaterThanOrEqual(3)
    expect(heatLabelColor(0, PALETTE)).toBe(PALETTE.labelDark)
  })
  it('无数据(灰底) → 深色字', () => {
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
    // 字色不再写死"红底白字", 而是按**底色亮度**选两端(2026-09-20) ⇒ 断言"看得见"
    expect(contrastOf(cells[0].label.color, cells[0].itemStyle.color)).toBeGreaterThanOrEqual(3)
    expect(cells[1].itemStyle.color).toBe(PALETTE.neutral)
    expect(cells[1].label.color).toBe(PALETTE.labelDark)
  })

  it('空列表 → 空数组(不抛)', () => {
    expect(toTreemapCells([], { palette: PALETTE, areaMetric: 'volume' })).toEqual([])
  })
})

// 2026-09-14 缺陷修复(P0 热力图空白): 后端整批返回 volume=0 时, 量能视图必须
// 要么给出正面积(保底可见)要么由调用方显式说明 —— 绝不允许 0 面积静默空白画布。
describe('toTreemapCells 量能全缺时的保底面积(空白画布根因)', () => {
  const zeros: BoardHeatItem[] = [
    item({ block_code: 'A', volume: 0, change_pct: -100, has_daily: false }),
    item({ block_code: 'B', volume: null, change_pct: null, has_daily: false }),
    // PG DECIMAL 经 JSON 变字符串的脏数(历史崩溃模式)也按缺失处理
    item({ block_code: 'C', volume: '0' as unknown as number, change_pct: null }),
  ]

  it('全部 volume=0/null/脏数 → 每块都拿到正保底面积(不会 0 面积消失)', () => {
    const cells = toTreemapCells(zeros, { palette: PALETTE, areaMetric: 'volume' })
    expect(cells).toHaveLength(3)
    for (const c of cells) {
      expect(Number.isFinite(c.value)).toBe(true)
      expect(c.value).toBeGreaterThan(0)
    }
    // 全缺时面积相等(等权保底), 且 ECharts 画得出来
    expect(new Set(cells.map((c) => c.value)).size).toBe(1)
    expect(hasDrawableArea(cells)).toBe(true)
  })

  it('旧行为对照: 只要保底面积落到 0, ECharts 就整块不画 —— hasDrawableArea 必须为 false', () => {
    const empty: ReturnType<typeof toTreemapCells> = []
    expect(hasDrawableArea(empty)).toBe(false)
    expect(hasDrawableArea(null)).toBe(false)
    const zeroCells = toTreemapCells(zeros, { palette: PALETTE, areaMetric: 'volume' }).map((c) => ({
      ...c,
      value: 0,
    }))
    expect(hasDrawableArea(zeroCells)).toBe(false)
  })

  it('hasUsableVolume / usableVolumeCount: 只有正有限数算可用量能', () => {
    expect(hasUsableVolume(zeros)).toBe(false)
    expect(usableVolumeCount(zeros)).toBe(0)
    const mixed = [...zeros, item({ block_code: 'D', volume: 1e10 })]
    expect(hasUsableVolume(mixed)).toBe(true)
    expect(usableVolumeCount(mixed)).toBe(1)
    expect(hasUsableVolume([item({ volume: Number.NaN })])).toBe(false)
    expect(hasUsableVolume([item({ volume: -5 })])).toBe(false)
    expect(hasUsableVolume([])).toBe(false)
  })

  it('混合场景保底仍按正值中位数 × minShare(全缺才退到固定保底)', () => {
    const cells = toTreemapCells(
      [item({ block_code: 'A', volume: 1_000_000 }), item({ block_code: 'B', volume: 0 })],
      { palette: PALETTE, areaMetric: 'volume' },
    )
    const a = cells.find((c) => c.blockCode === 'A')!.value
    const b = cells.find((c) => c.blockCode === 'B')!.value
    expect(a).toBe(1_000_000)
    expect(b).toBeGreaterThan(0)
    expect(b).toBeLessThan(a)
  })
})

describe('detectHeatAnomaly 板块异动规则 (盘中实时, 仅高亮不推送)', () => {
  it('涨速达标 → 急拉/急跌; 未达标 → null', () => {
    expect(detectHeatAnomaly({ speed: 0.6, volume_ratio: null })?.kinds).toEqual(['surge'])
    expect(detectHeatAnomaly({ speed: -0.8, volume_ratio: null })?.kinds).toEqual(['dump'])
    expect(detectHeatAnomaly({ speed: 0.3, volume_ratio: 1.1 })).toBeNull()
  })

  it('量比达标 → 放量; 与急拉并存时合并标签', () => {
    expect(detectHeatAnomaly({ speed: null, volume_ratio: 2.5 })?.kinds).toEqual(['heavy_volume'])
    const a = detectHeatAnomaly({ speed: 1.2, volume_ratio: 3 })
    expect(a?.kinds).toEqual(['surge', 'heavy_volume'])
    expect(a?.label).toBe('急拉 · 放量')
  })

  it('null/NaN/未达标量比 → 不误报', () => {
    expect(detectHeatAnomaly({ speed: null, volume_ratio: null })).toBeNull()
    expect(detectHeatAnomaly({ speed: Number.NaN, volume_ratio: Number.NaN })).toBeNull()
    expect(detectHeatAnomaly({ speed: undefined, volume_ratio: 1.9 })).toBeNull()
  })
})

describe('toTreemapCells 异动高亮', () => {
  it('异动块带 border 高亮与 anomaly 标注; 普通块无 border', () => {
    const cells = toTreemapCells(
      [
        item({ block_code: 'URFI-A', speed: 0.9, volume_ratio: 2.4 }),
        item({ block_code: 'URFI-B', speed: 0.1, volume_ratio: 1.0 }),
      ],
      { palette: PALETTE, areaMetric: 'equal' },
    )
    expect(cells[0].anomaly?.label).toBe('急拉 · 放量')
    expect(cells[0].itemStyle.borderWidth).toBe(2)
    expect(cells[1].anomaly).toBeNull()
    expect(cells[1].itemStyle.borderWidth ?? 0).toBe(0)
  })
})
