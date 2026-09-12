import { describe, expect, it } from 'vitest'
import {
  PHASE_LABEL,
  currentPhaseRule,
  phaseBandClass,
  phaseTextClass,
  type PhaseSegment,
  type PhaseStats,
} from '@/lib/market-phase'

const seg = (phase: string, start: string, end: string, days: number): PhaseSegment => ({
  phase: phase as PhaseSegment['phase'],
  label: PHASE_LABEL[phase as keyof typeof PHASE_LABEL],
  start,
  end,
  days,
  avg_height: 4,
  avg_first_board: 30,
  avg_ge2: 8,
  avg_promo: 0.2,
  avg_seal_rate: 0.6,
})

describe('phase 配色', () => {
  it('六阶段各有色带/文字档, 未知阶段回落到积累中', () => {
    expect(phaseBandClass('rally')).toBe('bg-red-500/70')
    expect(phaseBandClass('ice')).toBe('bg-blue-500/70')
    expect(phaseBandClass('nope')).toBe(phaseBandClass('accumulating'))
    expect(phaseTextClass('ebb')).toContain('orange')
    expect(phaseTextClass(null)).toBe(phaseTextClass('accumulating'))
  })
})

describe('currentPhaseRule', () => {
  const stats: Record<string, PhaseStats> = {
    repair: {
      count: 8, total_days: 208, max_days: 99, avg_days: 26, label: '修复',
      next: { ignite: 0.57, ebb: 0.29, rally: 0.14 },
      next_labels: { 启动: 0.57, 退潮: 0.29, 主升: 0.14 },
    },
  }
  it('给出当前阶段的历史段数/时长/去向', () => {
    const r = currentPhaseRule('repair', stats)
    expect(r).not.toBeNull()
    expect(r!.count).toBe(8)
    expect(r!.avg_days).toBe(26)
    expect(r!.max_days).toBe(99)
    expect(r!.next[0]).toEqual(['启动', 0.57])
  })
  it('当前阶段不在统计里 / 无统计 → null', () => {
    expect(currentPhaseRule('climax', stats)).toBeNull()
    expect(currentPhaseRule('repair', null)).toBeNull()
    expect(currentPhaseRule(null, stats)).toBeNull()
  })
})

describe('segments 形状', () => {
  it('段按 start 升序且天数自洽', () => {
    const segs = [seg('repair', '2026-09-01', '2026-09-05', 4), seg('ignite', '2026-09-08', '2026-09-09', 2)]
    expect(segs.map((s) => s.phase)).toEqual(['repair', 'ignite'])
    expect(segs[0].days).toBe(4)
  })
})
