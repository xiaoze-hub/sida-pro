// GS 区间色带 + 暗盘资金 纯逻辑单测(2026-10-11 决策先锋主图 §1 / 副图1 §2)。
import { describe, expect, it } from 'vitest'

import { gsZonePoints, GS_ZONE_COLOR } from '@panwatch/biz-ui/lib/gs-zones'
import { DARK_POOL_CONFIDENCE, darkPoolBarPoint, darkPoolCombo } from '@panwatch/biz-ui/lib/dark-pool'
import { readStockColors } from '@panwatch/biz-ui/lib/stock-colors'
import type { KlineInterval } from '@panwatch/biz-ui/lib/fund-bar'

const SC = readStockColors()
const INT: KlineInterval = '1d'

describe('gsZonePoints: 由已确认信号逐根推 GS 区间', () => {
  const bars = ['2026-01-01', '2026-01-02', '2026-01-03', '2026-01-04', '2026-01-05']

  it('首个已确认信号前不画, 信号当根起进入区间直到反向', () => {
    const sigs = [
      { date: '2026-01-02', side: 'G' as const, confirmed: true },
      { date: '2026-01-04', side: 'S' as const, confirmed: true },
    ]
    const pts = gsZonePoints(sigs, bars)
    expect(pts).toEqual([
      { date: '2026-01-02', zone: 'G' },
      { date: '2026-01-03', zone: 'G' },
      { date: '2026-01-04', zone: 'S' },
      { date: '2026-01-05', zone: 'S' },
    ])
  })

  it('未确认(pending)信号不据它染色(防末根假交叉)', () => {
    const sigs = [
      { date: '2026-01-02', side: 'G' as const, confirmed: true },
      { date: '2026-01-04', side: 'S' as const, confirmed: false }, // 盘中疑似
    ]
    const pts = gsZonePoints(sigs, bars)
    // 01-04 起仍是 G 区(S 未确认不算)
    expect(pts).toEqual([
      { date: '2026-01-02', zone: 'G' },
      { date: '2026-01-03', zone: 'G' },
      { date: '2026-01-04', zone: 'G' },
      { date: '2026-01-05', zone: 'G' },
    ])
  })

  it('无信号 / 空输入 → 空(不猜 G/S)', () => {
    expect(gsZonePoints([], bars)).toEqual([])
    expect(gsZonePoints(null, bars)).toEqual([])
    expect(gsZonePoints([{ date: '2026-01-02', side: 'G', confirmed: true }], [])).toEqual([])
  })

  it('日期乱序也按时间正确推区间', () => {
    const sigs = [
      { date: '2026-01-04', side: 'S' as const, confirmed: true },
      { date: '2026-01-02', side: 'G' as const, confirmed: true },
    ]
    const pts = gsZonePoints(sigs, bars)
    expect(pts).toEqual([
      { date: '2026-01-02', zone: 'G' },
      { date: '2026-01-03', zone: 'G' },
      { date: '2026-01-04', zone: 'S' },
      { date: '2026-01-05', zone: 'S' },
    ])
  })

  it('区带颜色: G区紫 / S区绿', () => {
    expect(GS_ZONE_COLOR.G).toMatch(/168, 85, 247/)
    expect(GS_ZONE_COLOR.S).toMatch(/34, 197, 94/)
  })
})

describe('darkPoolBarPoint: 红柱流入/绿柱流出, 0 不画', () => {
  it('净流入 → 红(up); 净流出 → 绿(down)', () => {
    const up = darkPoolBarPoint({ date: '2026-10-09', main_net: 123.0 }, INT, SC)
    expect(up && up.value).toBe(123.0)
    expect(up && up.color).toBe(SC.up)
    const dn = darkPoolBarPoint({ date: '2026-10-09', main_net: -456.0 }, INT, SC)
    expect(dn && dn.color).toBe(SC.down)
  })

  it('net=0 / null / NaN → null(不把 0 冒充成平衡柱)', () => {
    expect(darkPoolBarPoint({ date: '2026-10-09', main_net: 0 }, INT, SC)).toBeNull()
    expect(darkPoolBarPoint({ date: '2026-10-09', main_net: null }, INT, SC)).toBeNull()
    expect(darkPoolBarPoint({ date: '2026-10-09' }, INT, SC)).toBeNull()
    expect(darkPoolBarPoint({ date: '2026-10-09', main_net: NaN }, INT, SC)).toBeNull()
  })
})

describe('darkPoolCombo: 四组合用法(规格 §2)', () => {
  it('涨+红=持股待涨 / 跌+绿=回避减仓 / 跌+红=底部吸筹 / 涨+绿=诱多警惕', () => {
    expect(darkPoolCombo(1.0, 100)).toContain('持股待涨')
    expect(darkPoolCombo(-1.0, -100)).toContain('回避减仓')
    expect(darkPoolCombo(-1.0, 100)).toContain('底部吸筹')
    expect(darkPoolCombo(1.0, -100)).toContain('诱多')
  })

  it('涨跌为 0 或任一缺失 → null(不猜)', () => {
    expect(darkPoolCombo(0, 100)).toBeNull()
    expect(darkPoolCombo(1, 0)).toBeNull()
    expect(darkPoolCombo(null, 100)).toBeNull()
    expect(darkPoolCombo(1, null)).toBeNull()
  })

  it('confidence 常量 = L1_approx(诚实近似标注)', () => {
    expect(DARK_POOL_CONFIDENCE).toBe('L1_approx')
  })
})
