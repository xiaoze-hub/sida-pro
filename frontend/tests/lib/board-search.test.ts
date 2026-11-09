import { describe, expect, it } from 'vitest'
import type { BoardHeatItem } from '@sida/biz-ui/lib/board-heatmap'
import { normalizeBoardQuery, searchBoardMatches, searchBoards } from '@sida/biz-ui/lib/board-search'

// 板块查找(2026-11-09): 板块热力图 400+ 概念里定位单个板块的最小交互。
// 只做**本地已被后端返回的数据**上的过滤, 不编造条目/涨幅, 也不预置热门主题。
function item(over: Partial<BoardHeatItem> & Pick<BoardHeatItem, 'block_code' | 'name'>): BoardHeatItem {
  return {
    board_type: 'concept',
    change_pct: 1.23,
    fund_net: null,
    volume: null,
    date: '2026-11-07',
    has_daily: true,
    ...over,
  }
}

const ITEMS: BoardHeatItem[] = [
  item({ block_code: '880548', name: '商业航天', change_pct: 2.13 }),
  item({ block_code: '880549', name: '航天航空', change_pct: -0.42 }),
  item({ block_code: '880001', name: '低空经济', change_pct: 0.8 }),
  item({ block_code: '880002', name: 'AI算力', change_pct: null, has_daily: false }),
]

describe('searchBoards 板块查找', () => {
  it('空查询不返回任何条目(不把全量板块当结果倾倒)', () => {
    expect(searchBoards(ITEMS, '')).toEqual([])
    expect(searchBoards(ITEMS, '   ')).toEqual([])
  })

  it('按名称包含匹配: 商业航天能在概念列表里被定位', () => {
    const hits = searchBoards(ITEMS, '商业航天')
    expect(hits).toHaveLength(1)
    expect(hits[0]).toMatchObject({
      blockCode: '880548',
      name: '商业航天',
      changePct: 2.13,
      hasDaily: true,
      match: 'exact-name',
    })
  })

  it('按代码匹配(原板块代码可直接查)', () => {
    const hits = searchBoards(ITEMS, '880548')
    expect(hits.map((h) => h.blockCode)).toEqual(['880548'])
    expect(hits[0].match).toBe('exact-code')
  })

  it('英文名称忽略大小写', () => {
    expect(searchBoards(ITEMS, 'ai')[0]?.blockCode).toBe('880002')
  })

  it('精确名 > 前缀 > 包含, 同档保持后端原始顺序(不自造热度排名)', () => {
    const items = [
      item({ block_code: 'C1', name: '航天' }),
      item({ block_code: 'C2', name: '商业航天' }),
      item({ block_code: 'C3', name: '商业航天装备' }),
      item({ block_code: 'C4', name: '商业' }),
    ]
    const hits = searchBoards(items, '商业航天')
    expect(hits.map((h) => h.blockCode)).toEqual(['C2', 'C3'])
    const prefix = searchBoards(items, '商业')
    // 精确名「商业」排第一位; 其余前缀匹配保持后端原序(不自造热度排名)
    expect(prefix.map((h) => h.blockCode)).toEqual(['C4', 'C2', 'C3'])
  })

  it('没有匹配时返回空数组(由 UI 明说未找到)', () => {
    expect(searchBoards(ITEMS, '不存在的板块')).toEqual([])
  })

  it('原样保留 null 涨幅与 has_daily=false, 不补 0 不伪造', () => {
    const [hit] = searchBoards(ITEMS, 'AI算力')
    expect(hit.changePct).toBeNull()
    expect(hit.hasDaily).toBe(false)
  })

  it('limit 只截断结果, 不改变匹配语义', () => {
    const many = Array.from({ length: 30 }, (_, i) => item({ block_code: `B${i}`, name: `测试板块${i}` }))
    expect(searchBoards(many, '测试板块', { limit: 5 })).toHaveLength(5)
  })

  // 2026-11-09 复审: 展示上限(12)不是匹配总数。UI 必须能拿到真实 total 与是否截断。
  it('searchBoardMatches 给出真实匹配总数与截断标记, hits 仍按 limit 截断', () => {
    const many = Array.from({ length: 30 }, (_, i) => item({ block_code: `B${i}`, name: `测试板块${i}` }))
    const res = searchBoardMatches(many, '测试板块', { limit: 5 })
    expect(res.hits).toHaveLength(5)
    expect(res.total).toBe(30)
    expect(res.truncated).toBe(true)
  })

  it('未截断时 truncated=false, 总数等于命中数; 空查询/无匹配 total=0', () => {
    const few = searchBoardMatches(ITEMS, '航天', { limit: 12 })
    expect(few.total).toBe(few.hits.length)
    expect(few.truncated).toBe(false)
    expect(searchBoardMatches(ITEMS, '', { limit: 12 })).toEqual({ hits: [], total: 0, truncated: false })
    expect(searchBoardMatches(ITEMS, '不存在的板块', { limit: 12 })).toEqual({ hits: [], total: 0, truncated: false })
  })

  it('total 不受 limit=0 影响(仍报真实命中数)', () => {
    const res = searchBoardMatches(ITEMS, '航天', { limit: 0 })
    expect(res.hits).toHaveLength(0)
    expect(res.total).toBe(2)
    expect(res.truncated).toBe(true)
  })

  it('normalizeBoardQuery 去首尾空白并小写', () => {
    expect(normalizeBoardQuery('  商业航天 ')).toBe('商业航天')
    expect(normalizeBoardQuery(' AI ')).toBe('ai')
  })
})
