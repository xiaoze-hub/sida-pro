/**
 * 战术指令的时段排序（P0 门槛）：不同时段「今日推荐」不同，其余收进「更多」。
 */
import { describe, expect, it } from 'vitest'
import { orderPresetsForSession, sessionOf } from '../../src/lib/copilot/presets-order'

const PRESETS = [
  { id: 'full_dossier', group: 'dossier' },
  { id: 'auction_strong', group: 'auction' },
  { id: 'chip_cost', group: 'chip' },
  { id: 'resonance_first', group: 'resonance' },
  { id: 'demon_mainline', group: 'demon' },
  { id: 'defense_check', group: 'defense' },
]

describe('sessionOf', () => {
  const at = (h: number, m: number, day = 1) => {
    // 2026-10-01 是周四；用固定日期避免依赖真实时钟
    const d = new Date(2026, 9, day, h, m, 0)
    return d
  }

  it('盘前 / 竞价 / 盘中 / 盘后四档边界', () => {
    expect(sessionOf(at(8, 30))).toBe('preopen')
    expect(sessionOf(at(9, 14))).toBe('preopen')
    expect(sessionOf(at(9, 15))).toBe('auction')
    expect(sessionOf(at(9, 29))).toBe('auction')
    expect(sessionOf(at(9, 30))).toBe('intraday')
    expect(sessionOf(at(15, 0))).toBe('intraday')
    expect(sessionOf(at(15, 1))).toBe('closed')
  })

  it('周末一律按盘后', () => {
    expect(sessionOf(new Date(2026, 9, 3, 10, 0, 0))).toBe('closed') // 周六
  })
})

describe('orderPresetsForSession', () => {
  it('竞价时段把竞价放第一位', () => {
    const { primary } = orderPresetsForSession(PRESETS, 'auction')
    expect(primary[0].id).toBe('auction_strong')
    expect(primary).toHaveLength(3)
  })

  it('盘中优先全景诊断与防守位', () => {
    const { primary, more } = orderPresetsForSession(PRESETS, 'intraday')
    expect(primary.map((p) => p.id)).toEqual(['full_dossier', 'defense_check', 'chip_cost'])
    expect(more).toHaveLength(3)
  })

  it('盘前先看全貌再看埋伏', () => {
    const { primary } = orderPresetsForSession(PRESETS, 'preopen')
    expect(primary[0].id).toBe('full_dossier')
    expect(primary[1].id).toBe('chip_cost')
  })

  it('无 group 的自定义指令排最后，不抢推荐位', () => {
    const withCustom = [...PRESETS, { id: 'zz_custom', group: undefined }]
    const { primary } = orderPresetsForSession(withCustom, 'intraday')
    expect(primary.map((p) => p.id)).not.toContain('zz_custom')
  })

  it('排序稳定：同样输入两次结果一致（避免刷新跳动）', () => {
    const a = orderPresetsForSession(PRESETS, 'closed')
    const b = orderPresetsForSession([...PRESETS].reverse(), 'closed')
    expect(a.primary.map((p) => p.id)).toEqual(b.primary.map((p) => p.id))
  })
})
