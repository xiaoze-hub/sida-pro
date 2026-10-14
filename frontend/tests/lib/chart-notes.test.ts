// 图表说明(chart-notes)纯逻辑单测(2026-10-14 看图决策产品件)。
//
// 钉住「为什么」文案 / 白名单清洗 / 图下说明条 三条口味的确定性:
//  - 缺数据显式(不补 0/不编);
//  - 脏点不进条(白名单过滤);
//  - 悬停/图例/说明条共用同一 `markerWhy`(AI 读的=图上标的)。
import { describe, expect, it } from 'vitest'

import {
  markerWhy,
  normalizeChartNotes,
  normalizeChartNotesPayload,
  noteForMarker,
  notesBarText,
  notesForDate,
  zoneNoteText,
  type ChartNote,
} from '../../packages/biz-ui/src/lib/chart-notes'

function note(over: Partial<ChartNote> = {}): ChartNote {
  return {
    kind: 'gs',
    markerType: 'GS买卖点',
    label: 'G 买入',
    date: '2026-03-02',
    price: 9.8,
    side: 'G',
    index: null,
    trigger: ['A0 上穿 BB0 → G 买入'],
    asOf: '2026-03-02',
    asOfIsToday: false,
    asOfNote: '数据时点: 2026-03-02(非今日, 基准日滞后)',
    invalidation: ['若出现反向 GS 交叉(S), 则该 G 区间结束'],
    invalidationDefaulted: true,
    confidence: { calibrated: false, note: '规则证据, 未校准' },
    id: 'gs:2026-03-02:-',
    ...over,
  }
}

describe('normalizeChartNotes 白名单清洗', () => {
  it('脏点不进条: 缺 kind 或缺 trigger 的被剔除', () => {
    const raw = [
      { kind: 'gs', trigger: ['ok'], marker_type: 'GS买卖点' },
      { kind: '', trigger: ['x'] }, // 缺 kind
      { kind: 'pattern', trigger: [] }, // 空 trigger
      null,
      'nope',
    ]
    const out = normalizeChartNotes(raw)
    expect(out).toHaveLength(1)
    expect(out[0].kind).toBe('gs')
  })

  it('缺字段显式(price 非数 → null, 不补 0)', () => {
    const out = normalizeChartNotes([{ kind: 'gs', trigger: ['a'], price: 'x' }])
    expect(out[0].price).toBeNull()
    expect(out[0].date).toBeNull()
    expect(out[0].invalidation).toEqual([])
  })

  it('非数组 → 空数组(不编)', () => {
    expect(normalizeChartNotes(null)).toEqual([])
    expect(normalizeChartNotes({})).toEqual([])
  })
})

describe('markerWhy 一行式「为什么」', () => {
  it('三字段齐全: 触发/时点/失效', () => {
    const s = markerWhy(note())
    expect(s).toContain('触发: A0 上穿 BB0')
    expect(s).toContain('时点: 数据时点: 2026-03-02')
    expect(s).toContain('失效: 若出现反向 GS 交叉')
  })

  it('缺字段显式, 不空', () => {
    const s = markerWhy(note({ trigger: [], asOfNote: '', invalidation: [] }))
    expect(s).toContain('触发条件缺失')
    expect(s).toContain('数据时点缺失')
    expect(s).toContain('失效条件缺失')
  })
})

describe('notesForDate 悬停读数', () => {
  const notes = [
    note({ date: '2026-03-02' }),
    note({ date: '2026-03-07', kind: 'pattern' }),
    note({ date: null }),
  ]
  it('按交易日过滤(取前 10 位)', () => {
    expect(notesForDate(notes, '2026-03-07')).toHaveLength(1)
    expect(notesForDate(notes, '2026-03-07T00:00:00')).toHaveLength(1)
    expect(notesForDate(notes, null)).toEqual([])
  })
})

describe('notesBarText 图下说明条(一行式)', () => {
  it('取最近 limit 条拼句', () => {
    const mk = (date: string) => note({ date, asOfNote: '数据时点: 今日' })
    const notes = [mk('2026-03-02'), mk('2026-03-05'), mk('2026-03-07')]
    const s = notesBarText(notes, 2)
    expect(s).toContain('2026-03-05')
    expect(s).toContain('2026-03-07')
    expect(s).not.toContain('2026-03-02')
  })

  it('无说明 → 空串(调用方据此不渲染)', () => {
    expect(notesBarText([], 3)).toBe('')
  })
})

describe('zoneNoteText 当前 GS 标记说明', () => {
  it('有说明 → 一行式; 无 → 空串', () => {
    const n = note({ label: 'G区区间', kind: 'gs' })
    expect(zoneNoteText(n)).toContain('G区区间')
    expect(zoneNoteText(n)).toContain('触发:')
    expect(zoneNoteText(null)).toBe('')
  })
})

describe('noteForMarker 图例悬停匹配', () => {
  const notes = [
    note({ kind: 'pattern', date: '2026-03-07', label: '金针探底 看涨' }),
    note({ kind: 'gs', date: '2026-03-07', label: 'S 卖出' }),
  ]
  it('按 类型+日期+标签前缀 命中', () => {
    expect(noteForMarker(notes, 'pattern', '2026-03-07', '金针探底')?.label).toBe('金针探底 看涨')
    expect(noteForMarker(notes, 'gs', '2026-03-07')?.label).toBe('S 卖出')
  })
  it('找不到 → null(不编)', () => {
    expect(noteForMarker(notes, 'pattern', '2026-03-08')).toBeNull()
  })
})

describe('normalizeChartNotesPayload 整包', () => {
  it('notes + zone_note + count/notes_text 齐; 缺 notes_text 用 markerWhy 兜', () => {
    const raw = {
      count: 1,
      notes: [{ kind: 'gs', trigger: ['a'], marker_type: 'GS买卖点', label: 'G 买入', date: '2026-03-02' }],
      zone_note: { kind: 'gs', trigger: ['z'], marker_type: 'GS买卖点', label: 'G区区间' },
    }
    const p = normalizeChartNotesPayload(raw)
    expect(p.notes).toHaveLength(1)
    expect(p.zoneNote?.label).toBe('G区区间')
    expect(p.count).toBe(1)
    expect(p.notesText[0]).toContain('触发:')
  })

  it('空 → 空壳(不编造)', () => {
    const p = normalizeChartNotesPayload(null)
    expect(p.notes).toEqual([])
    expect(p.zoneNote).toBeNull()
  })
})
