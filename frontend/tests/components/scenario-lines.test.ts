// 情景关键位图层(2026-10-22)—— 纯函数单测。
//
// 覆盖: 正常响应 → 4 条线且按价格从高到低排序; 同价位去重; available:false → 空数组;
//       脏值(NaN / 负价 / 偏离现价 80%)被丢弃且 dropped 计数正确。
//
// 口径钉死(前端只画后端算好的关键位, 不重算方向):
//   - 阻力/走强目标 = rose(涨红), 支撑/走弱目标 = emerald(跌绿) —— 中国习惯买红卖绿;
//   - 去重: 同价位只留一条, 结构性关键位(阻力/支撑)优先于情景目标位;
//   - 脏值: NaN / <=0 / 与现价偏离 > 50% 一律丢弃。
import { describe, expect, it } from 'vitest'

import {
  buildScenarioLines,
  buildScenarioLineResult,
  SCENARIO_LINE_LABEL,
  SCENARIO_LINE_TONE,
  SCENARIO_MAX_DEVIATION,
  type ScenarioLine,
} from '@sida/biz-ui/lib/scenario-lines'
import type { ScenarioMatrixResponse } from '@sida/api'

/** 后端正常响应(与 `/klines/{symbol}/scenario` 的结构一致)。 */
const NORMAL: ScenarioMatrixResponse = {
  available: true,
  price: 32.93,
  as_of: '2026-10-21',
  levels: {
    resist: 33.92,
    support: 32.92,
    ma20: 33.9,
    ma60: 35.9,
    band_hi: 44.5,
    band_lo: 32.5,
  },
  scenarios: [
    { id: 'strong', label: '走强', trigger: '收盘站上 33.92', target: 45.72, action: '放量突破', invalid: '假突破' },
    { id: 'flat', label: '震荡', trigger: '区间内', target: '32.92 ~ 33.92', action: '上沿减', invalid: '有效突破' },
    { id: 'weak', label: '走弱', trigger: '收盘跌破 32.92', target: 30.5, action: '按纪律减仓', invalid: '次日收回' },
  ],
}

describe('buildScenarioLines — 正常响应', () => {
  it('出 4 条线(阻力/支撑/走强目标/走弱目标)', () => {
    const lines = buildScenarioLines(NORMAL)
    expect(lines).toHaveLength(4)
    expect(lines.map((l) => l.kind).sort()).toEqual(
      ['resist', 'support', 'target_strong', 'target_weak'].sort(),
    )
  })

  it('按价格从高到低排序', () => {
    const lines = buildScenarioLines(NORMAL)
    const prices = lines.map((l) => l.price)
    expect(prices).toEqual([...prices].sort((a, b) => b - a))
    expect(prices).toEqual([45.72, 33.92, 32.92, 30.5])
  })

  it('label 用中文位名(阻力/支撑/走强目标/走弱目标)', () => {
    const lines = buildScenarioLines(NORMAL)
    expect(lines.map((l) => l.label)).toEqual(
      expect.arrayContaining(['阻力', '支撑', '走强目标', '走弱目标']),
    )
    for (const l of lines) {
      expect(l.label).toBe(SCENARIO_LINE_LABEL[l.kind])
    }
  })

  it('颜色语义: 阻力/走强目标=rose(涨红), 支撑/走弱目标=emerald(跌绿) —— 买红卖绿', () => {
    const lines = buildScenarioLines(NORMAL)
    const byKind = Object.fromEntries(lines.map((l) => [l.kind, l.color]))
    expect(byKind.resist).toBe('rose')
    expect(byKind.target_strong).toBe('rose')
    expect(byKind.support).toBe('emerald')
    expect(byKind.target_weak).toBe('emerald')
    expect(SCENARIO_LINE_TONE.resist).toBe('rose')
    expect(SCENARIO_LINE_TONE.support).toBe('emerald')
  })

  it('正常响应没有脏值 → dropped = 0', () => {
    const { dropped } = buildScenarioLineResult(NORMAL)
    expect(dropped).toBe(0)
  })
})

describe('buildScenarioLines — available:false', () => {
  it('关键位不足 → 空数组(静默不画), dropped = 0', () => {
    const lines = buildScenarioLines({
      available: false,
      reason: '关键位不足（缺均线/筹码带/区间高低）',
    })
    expect(lines).toEqual([])
    expect(lines).toHaveLength(0)
    expect(buildScenarioLineResult({ available: false }).dropped).toBe(0)
  })

  it('null / undefined 响应 → 空数组', () => {
    expect(buildScenarioLines(null)).toEqual([])
    expect(buildScenarioLines(undefined)).toEqual([])
  })
})

describe('buildScenarioLines — 去重', () => {
  it('同价位只留一条(结构性关键位优先于情景目标位)', () => {
    const lines = buildScenarioLines({
      available: true,
      price: 32.93,
      levels: { resist: 33.92, support: 32.92 },
      scenarios: [
        { id: 'strong', target: 33.92 }, // 与阻力同价 → 被去重
        { id: 'weak', target: 32.92 }, // 与支撑同价 → 被去重
      ],
    })
    expect(lines).toHaveLength(2)
    expect(lines.map((l) => l.kind)).toEqual(['resist', 'support'])
  })

  it('同价位且都是目标位 → 留先出现的那条(走强目标优先于走弱目标)', () => {
    const lines = buildScenarioLines({
      available: true,
      price: 32.93,
      levels: { resist: 33.92, support: 32.0 },
      scenarios: [
        { id: 'strong', target: 40 },
        { id: 'weak', target: 40 },
      ],
    })
    expect(lines.filter((l) => l.price === 40)).toHaveLength(1)
    expect(lines.find((l) => l.price === 40)?.kind).toBe('target_strong')
  })

  it('价格差在小数点两位内视为同价位(去重)', () => {
    const lines = buildScenarioLines({
      available: true,
      price: 32.93,
      levels: { resist: 33.924, support: 32.0 },
      scenarios: [{ id: 'strong', target: 33.92 }],
    })
    expect(lines).toHaveLength(2)
  })
})

describe('buildScenarioLines — 脏值清洗', () => {
  it('NaN / 负价 / 0 被丢弃, dropped 计数正确', () => {
    const { lines, dropped } = buildScenarioLineResult({
      available: true,
      price: 32.93,
      levels: { resist: Number.NaN, support: -5 },
      scenarios: [
        { id: 'strong', target: 45.72 },
        { id: 'weak', target: 0 }, // 0 不是有效价位
      ],
    })
    // resist(NaN) + support(-5) + target_weak(0) 三条被丢
    expect(dropped).toBe(3)
    expect(lines).toHaveLength(1)
    expect(lines[0].kind).toBe('target_strong')
    expect(lines[0].price).toBe(45.72)
  })

  it('与现价偏离 80% 的极值视为脏数据丢弃, dropped 计数正确', () => {
    const { lines, dropped } = buildScenarioLineResult({
      available: true,
      price: 32.93,
      levels: { resist: 33.92, support: 32.92 },
      scenarios: [
        { id: 'strong', target: 45.72 },
        { id: 'weak', target: 59.28 }, // 32.93 * 1.8 ≈ 59.27 → 偏离 80%
      ],
    })
    expect(dropped).toBe(1)
    expect(lines).toHaveLength(3)
    expect(lines.some((l) => l.price === 59.28)).toBe(false)
  })

  it('偏离阈值边界: 恰好 50% 保留, 超过即丢', () => {
    const at = 32.93 * (1 + SCENARIO_MAX_DEVIATION)
    const over = 32.93 * (1 + SCENARIO_MAX_DEVIATION + 0.01)
    const keep = buildScenarioLineResult({
      available: true,
      price: 32.93,
      levels: { resist: at, support: 32.0 },
      scenarios: [],
    })
    expect(keep.dropped).toBe(0)
    expect(keep.lines.some((l) => l.price === at)).toBe(true)

    const drop = buildScenarioLineResult({
      available: true,
      price: 32.93,
      levels: { resist: over, support: 32.0 },
      scenarios: [],
    })
    expect(drop.dropped).toBe(1)
  })

  it('非数值目标位(如"下方无明确支撑")被丢弃', () => {
    const { lines, dropped } = buildScenarioLineResult({
      available: true,
      price: 32.93,
      levels: { resist: 33.92, support: 32.92 },
      scenarios: [{ id: 'weak', target: '下方无明确支撑（需重新评估）' }],
    })
    expect(dropped).toBe(1)
    expect(lines).toHaveLength(2)
  })

  it('字符串数字目标位可解析(如 "32.5")', () => {
    const lines = buildScenarioLines({
      available: true,
      price: 32.93,
      levels: { resist: 33.92, support: 32.92 },
      scenarios: [{ id: 'weak', target: '32.5' }],
    })
    expect(lines.some((l) => l.kind === 'target_weak' && l.price === 32.5)).toBe(true)
  })

  it('现价缺失时只过 NaN/<=0 那关(不做偏离判定)', () => {
    const { lines, dropped } = buildScenarioLineResult({
      available: true,
      levels: { resist: 33.92, support: 32.92 },
      scenarios: [{ id: 'strong', target: 999 }],
    })
    expect(dropped).toBe(0)
    expect(lines).toHaveLength(3)
  })
})

describe('buildScenarioLines — 真实后端形态', () => {
  /**
   * 后端 `scenario_engine.build_scenarios` 实际产出的是 **`name`** 字段(走强/震荡/走弱),
   * 没有 `id`; 震荡档的 target 是区间字符串("32.5 ~ 36.0")。
   * 这条用例把该契约钉死 —— 前端必须同时吃 `id` 形态与 `name` 形态。
   */
  it('name 形态(走强/震荡/走弱)照样出 4 条线, 震荡档不产线', () => {
    const lines = buildScenarioLines({
      available: true,
      price: 32.93,
      levels: { price: 32.93, resist: 36.0, support: 32.5, band_hi: 44.5, band_lo: 32.5 },
      scenarios: [
        { name: '走强', trigger: '收盘站上 36.0', target: 44.5 },
        { name: '震荡', trigger: '在 32.5 ~ 36.0 之间震荡', target: '32.5 ~ 36.0' },
        { name: '走弱', trigger: '收盘跌破 32.5', target: 32.0 },
      ],
    })
    expect(lines).toHaveLength(4)
    expect(lines.map((l) => l.kind)).toEqual([
      'target_strong', // 44.5
      'resist', // 36.0
      'support', // 32.5
      'target_weak', // 32.0
    ])
    expect(lines.dropped).toBe(0)
  })
})

describe('buildScenarioLines — 返回值形状', () => {
  it('是数组(可直接 map/渲染), 且挂 dropped 计数', () => {
    const lines = buildScenarioLines(NORMAL)
    expect(Array.isArray(lines)).toBe(true)
    expect(lines.dropped).toBe(0)
    expect(lines.lines).toBe(lines)
  })

  it('每条线都带 price/kind/label/color 四字段', () => {
    for (const l of buildScenarioLines(NORMAL) as ScenarioLine[]) {
      expect(typeof l.price).toBe('number')
      expect(Number.isFinite(l.price)).toBe(true)
      expect(l.price).toBeGreaterThan(0)
      expect(['resist', 'support', 'target_strong', 'target_weak']).toContain(l.kind)
      expect(typeof l.label).toBe('string')
      expect(['rose', 'emerald']).toContain(l.color)
    }
  })
})
