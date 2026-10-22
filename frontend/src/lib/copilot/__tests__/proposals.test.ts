// @vitest-environment node
/**
 * 提案面板纯函数回归（2026-10-22）。
 *
 * 钉住三件最容易做错的事：
 *  ① **排序**：待审按 `seen_count` 降序（同次数按 last_seen_at 降序、再按后端顺序=稳定）；
 *     其余 tab 保持后端时间序。
 *  ② **空值**：`seen_count` 缺失 → `×--`（绝不补 0）；元信息缺项整段省略。
 *  ③ **文案**：状态/动作中文口径固定（采纳 ≠ 已生效，见组件头注）。
 */
import { describe, expect, it } from 'vitest'

import {
  ACTION_LABEL,
  PROPOSAL_TABS,
  buildCandidateToolsLine,
  buildMetaLine,
  buildSummaryItems,
  buildTopLine,
  clipEvidence,
  formatFirstSeen,
  formatSeenCount,
  isEmptyOverall,
  normalizeStatus,
  sortProposals,
  statusLabel,
  type ProposalRow,
  type ProposalStats,
} from '@/lib/copilot/proposals'

function row(patch: Partial<ProposalRow> & { id: number }): ProposalRow {
  return {
    dimension: `维度${patch.id}`,
    status: 'pending',
    seen_count: 1,
    first_seen_at: '2026-10-20 09:00:00',
    last_seen_at: '2026-10-20 09:00:00',
    ...patch,
  }
}

describe('formatSeenCount', () => {
  it('有值 → ×N', () => {
    expect(formatSeenCount(5)).toBe('×5')
    expect(formatSeenCount('3')).toBe('×3')
  })

  it('缺失 → --（禁止补 0）', () => {
    expect(formatSeenCount(null)).toBe('--')
    expect(formatSeenCount(undefined)).toBe('--')
    expect(formatSeenCount('')).toBe('--')
    expect(formatSeenCount('abc')).toBe('--')
  })

  it('真实 0 照实显示（0 是值，缺失才是 --）', () => {
    expect(formatSeenCount(0)).toBe('×0')
  })
})

describe('sortProposals', () => {
  it('待审：按 seen_count 降序，与后端的 last_seen_at 顺序无关', () => {
    const rows = [
      row({ id: 1, seen_count: 1, last_seen_at: '2026-10-22 10:00:00' }),
      row({ id: 2, seen_count: 5, last_seen_at: '2026-10-20 10:00:00' }),
      row({ id: 3, seen_count: 2, last_seen_at: '2026-10-21 10:00:00' }),
    ]
    expect(sortProposals(rows, 'pending').map((r) => r.id)).toEqual([2, 3, 1])
  })

  it('待审：次数相同按 last_seen_at 降序', () => {
    const rows = [
      row({ id: 1, seen_count: 3, last_seen_at: '2026-10-20 10:00:00' }),
      row({ id: 2, seen_count: 3, last_seen_at: '2026-10-22 10:00:00' }),
      row({ id: 3, seen_count: 3, last_seen_at: '2026-10-21 10:00:00' }),
    ]
    expect(sortProposals(rows, 'pending').map((r) => r.id)).toEqual([2, 3, 1])
  })

  it('待审：次数与时间全同时保持后端顺序（稳定排序）', () => {
    const rows = [
      row({ id: 11, seen_count: 2, last_seen_at: '2026-10-20 10:00:00' }),
      row({ id: 12, seen_count: 2, last_seen_at: '2026-10-20 10:00:00' }),
      row({ id: 13, seen_count: 2, last_seen_at: '2026-10-20 10:00:00' }),
    ]
    expect(sortProposals(rows, 'pending').map((r) => r.id)).toEqual([11, 12, 13])
  })

  it('待审：seen_count 缺失排最后（不当作 0 参与比较）', () => {
    const rows = [
      row({ id: 1, seen_count: null }),
      row({ id: 2, seen_count: 1 }),
      row({ id: 3, seen_count: 4 }),
    ]
    expect(sortProposals(rows, 'pending').map((r) => r.id)).toEqual([3, 2, 1])
  })

  it('其余 tab 保持后端时间序（不按次数重排）', () => {
    const rows = [
      row({ id: 1, status: 'accept', seen_count: 1, last_seen_at: '2026-10-22 10:00:00' }),
      row({ id: 2, status: 'accept', seen_count: 9, last_seen_at: '2026-10-20 10:00:00' }),
    ]
    expect(sortProposals(rows, 'accept').map((r) => r.id)).toEqual([1, 2])
    expect(sortProposals(rows, 'all').map((r) => r.id)).toEqual([1, 2])
  })

  it('不改动入参（返回新数组）', () => {
    const rows = [row({ id: 1, seen_count: 1 }), row({ id: 2, seen_count: 2 })]
    const out = sortProposals(rows, 'pending')
    expect(rows.map((r) => r.id)).toEqual([1, 2])
    expect(out).not.toBe(rows)
  })

  it('非数组入参不炸（后端异常时返回空列表）', () => {
    // @ts-expect-error 故意传非法值，守住运行时韧性
    expect(sortProposals(null, 'pending')).toEqual([])
  })
})

describe('时间与元信息', () => {
  it('formatFirstSeen：空格/ISO 两种写法都认，认不出给 null', () => {
    expect(formatFirstSeen('2026-10-20 09:00:00')).toBe('10-20')
    expect(formatFirstSeen('2026-10-20T09:00:00+08:00')).toBe('10-20')
    expect(formatFirstSeen('')).toBeNull()
    expect(formatFirstSeen(null)).toBeNull()
    expect(formatFirstSeen('上周')).toBeNull()
  })

  it('buildMetaLine：三项齐全', () => {
    expect(
      buildMetaLine({ symbol: '600111', recipe: 'demon_diagnosis', first_seen_at: '2026-10-20 09:00:00' }),
    ).toBe('600111 · demon_diagnosis · 10-20 起')
  })

  it('buildMetaLine：缺项整段省略（不出现 -- · --）', () => {
    expect(buildMetaLine({ symbol: null, recipe: 'demon_diagnosis', first_seen_at: '2026-10-20' })).toBe(
      'demon_diagnosis · 10-20 起',
    )
    expect(buildMetaLine({ symbol: '600111', recipe: '  ', first_seen_at: null })).toBe('600111')
  })

  it('buildMetaLine：三项全缺 → null（整行不渲染）', () => {
    expect(buildMetaLine({ symbol: '', recipe: null, first_seen_at: undefined })).toBeNull()
  })
})

describe('clipEvidence', () => {
  it('短证据原样返回', () => {
    expect(clipEvidence('十大股东里看不到户数')).toBe('十大股东里看不到户数')
  })

  it('超 200 字截断并补省略号', () => {
    const long = 'a'.repeat(240)
    const out = clipEvidence(long)
    expect(out).not.toBeNull()
    expect(Array.from(out ?? '').length).toBe(201) // 200 字符 + 省略号
    expect(out?.endsWith('…')).toBe(true)
  })

  it('空/缺 → null（行不可展开）', () => {
    expect(clipEvidence('')).toBeNull()
    expect(clipEvidence('   ')).toBeNull()
    expect(clipEvidence(null)).toBeNull()
  })
})

describe('文案口径', () => {
  it('statusLabel：四态中文 + 未知原样 + 缺失 --', () => {
    expect(statusLabel('pending')).toBe('待审')
    expect(statusLabel('accept')).toBe('采纳')
    expect(statusLabel('reject')).toBe('忽略')
    expect(statusLabel('snooze')).toBe('搁置')
    expect(statusLabel('weird')).toBe('weird')
    expect(statusLabel(null)).toBe('--')
  })

  it('normalizeStatus 只小写不改写语义', () => {
    expect(normalizeStatus(' ACCEPT ')).toBe('accept')
    expect(normalizeStatus(undefined)).toBe('')
  })

  it('动作文案与状态文案同字（用户不会看到两套叫法）', () => {
    expect(ACTION_LABEL.accept).toBe(statusLabel('accept'))
    expect(ACTION_LABEL.reject).toBe(statusLabel('reject'))
    expect(ACTION_LABEL.snooze).toBe(statusLabel('snooze'))
  })

  it('过滤带：待审在前，全部收尾', () => {
    expect(PROPOSAL_TABS.map((t) => t.value)).toEqual(['pending', 'accept', 'reject', 'snooze', 'all'])
    expect(PROPOSAL_TABS.map((t) => t.label)).toEqual(['待审', '采纳', '忽略', '搁置', '全部'])
  })
})

describe('汇总带', () => {
  const summary = { total: 6, pending: 3, accept: 2, reject: 1, snooze: 0 }

  it('buildSummaryItems：固定四行顺序 + 真实 0 显示 0', () => {
    expect(buildSummaryItems(summary)).toEqual([
      { key: 'pending', label: '待审', value: '3' },
      { key: 'accept', label: '采纳', value: '2' },
      { key: 'reject', label: '忽略', value: '1' },
      { key: 'snooze', label: '搁置', value: '0' },
    ])
  })

  it('buildSummaryItems：summary 缺失一律 --（不是 0）', () => {
    expect(buildSummaryItems(null).map((i) => i.value)).toEqual(['--', '--', '--', '--'])
  })

  it('buildTopLine：维度名 + 次数；top 缺失 → null（不编假条目）', () => {
    const stats = {
      items: [],
      top: { dimension: '股东户数变化', seen_count: 5 },
      summary,
    } as ProposalStats
    expect(buildTopLine(stats)).toEqual({ dimension: '股东户数变化', count: '×5' })
    expect(buildTopLine({ ...stats, top: null })).toBeNull()
    expect(buildTopLine(null)).toBeNull()
    // 维度名为空也不算「最该补」—— 一行空白比没有更糟
    expect(buildTopLine({ ...stats, top: { dimension: '  ', seen_count: 5 } })).toBeNull()
  })

  it('isEmptyOverall：只有明确 total=0 才算全局空', () => {
    expect(isEmptyOverall({ items: [], top: null, summary: { total: 0, pending: 0, accept: 0, reject: 0, snooze: 0 } })).toBe(
      true,
    )
    expect(isEmptyOverall({ items: [], top: null, summary })).toBe(false)
    // stats 拿不到 = 不知道，不能断言「没有提案」
    expect(isEmptyOverall(null)).toBe(false)
  })

  it('buildCandidateToolsLine：有候选列名字，没候选/字段缺失一律不显示', () => {
    expect(
      buildCandidateToolsLine(
        row({
          id: 1,
          candidate_tools: [{ name: 'get_fundamentals_detail' }, { name: 'get_northbound', caliber: 'x' }],
        }),
      ),
    ).toBe('可挂载现有工具：get_fundamentals_detail、get_northbound')
    // 0 个候选 / null / 字段缺失 → 空串（不写「无」，那会被读成「平台没有」）
    expect(buildCandidateToolsLine(row({ id: 2, candidate_tools: [] }))).toBe('')
    expect(buildCandidateToolsLine(row({ id: 3, candidate_tools: null }))).toBe('')
    expect(buildCandidateToolsLine(row({ id: 4 }))).toBe('')
    // 空名字不算候选
    expect(buildCandidateToolsLine(row({ id: 5, candidate_tools: [{ name: '  ' }] }))).toBe('')
  })
})
