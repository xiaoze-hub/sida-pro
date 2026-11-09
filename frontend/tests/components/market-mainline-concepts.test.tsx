// @vitest-environment jsdom
import { cleanup, render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 首页「市场主线」概念维度接入 (2026-11-09, 后端 8403e2bf)。
 *
 * 这些用例钉的是**组件真实行为**, 不是文案存在性:
 *   1) 后端只给行业榜时, 概念页必须为空 —— 绝不用行业数据冒充概念;
 *   2) 概念数据在时, 缺省视图能直接看到热点概念(不被行业榜遮住);
 *   3) 概念口径(不可相加/时点/来源/龙头仅组内排序)必须原样上屏;
 *   4) 时点缺失 → 显示"时点未知"而不是猜成当天。
 */

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))

import MarketMainlineCard from '@sida/biz-ui/components/MarketMainlineCard'

type Group = {
  name: string
  dimension?: 'industry' | 'concept'
  limit_up_count: number
  ge2_count: number
  max_boards: number
  boards_sum: number
  rungs: number
  score: number | null
  leader: { code: string; name: string; days: number; amount: number }
  constituents: Array<{ code: string; name: string; days: number; amount: number; concept_count?: number }>
  leader_shared_concepts?: string[]
  leader_basis?: string
  exclusive_ratio?: number
}

function industryGroup(name: string): Group {
  return {
    name,
    dimension: 'industry',
    limit_up_count: 8,
    ge2_count: 3,
    max_boards: 4,
    boards_sum: 12,
    rungs: 3,
    score: 72.5,
    leader: { code: '600001', name: `${name}龙头`, days: 4, amount: 3.2e8 },
    constituents: [{ code: '600001', name: `${name}龙头`, days: 4, amount: 3.2e8 }],
  }
}

function conceptGroup(name: string): Group {
  return {
    name,
    dimension: 'concept',
    limit_up_count: 6,
    ge2_count: 2,
    max_boards: 5,
    boards_sum: 13,
    rungs: 3,
    score: 81.2,
    leader: { code: '300900', name: '航天龙头', days: 5, amount: 8.8e8 },
    constituents: [
      { code: '300900', name: '航天龙头', days: 5, amount: 8.8e8, concept_count: 3 },
      { code: '300901', name: '配套甲', days: 2, amount: 2.1e8, concept_count: 1 },
    ],
    leader_shared_concepts: ['卫星互联网', '军工电子'],
    leader_basis: '本概念内连板数优先、成交额次之排序第一；非该股专属驱动',
    exclusive_ratio: 0.5,
  }
}

function baseResp(over: Record<string, unknown> = {}) {
  return {
    total_groups: 3,
    total_limit_ups: 21,
    min_limitup_for_rank: 3,
    ranked_groups: [industryGroup('半导体')],
    unranked: [],
    filter_stats: { broad_filtered: 1, below_min: 2, ranked: 1 },
    dimension: 'industry',
    concept_groups: [conceptGroup('商业航天')],
    concept_unranked: [],
    concept_stats: {
      distinct_stocks: 12,
      tagged_stocks: 12,
      untagged_stocks: 9,
      multi_concept_stocks: 4,
      member_count_sum: 17,
      overlap_excess: 5,
      overlap_ratio: 0.29,
      broad_filtered: 2,
      summable: false,
      min_limitup_for_rank: 3,
      note: '概念为独立维度：一只股票可同时属于多个概念，各概念涨停家数不可相加（相加会重复计同一只票）。',
    },
    data_meta: {
      as_of: '2026-11-06',
      computed_at: '2026-11-09T11:09:12+08:00',
      pool_source: 'tq',
      concept_source: 'tq:get_relation',
      as_of_note: '',
    },
    note: '',
    ...over,
  }
}

beforeEach(() => {
  mocks.fetchAPI.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('市场主线 · 概念维度接入', () => {
  it('涨停家数靠前但综合分在第十一的概念仍在默认视图可见', async () => {
    const groups = Array.from({ length: 10 }, (_, i) => ({ ...conceptGroup(`题材${i}`), limit_up_count: 3 }))
    groups.push({ ...conceptGroup('商业航天'), limit_up_count: 13 })
    mocks.fetchAPI.mockResolvedValue(baseResp({ concept_groups: groups, concept_breadth_leaders: [{ name: '商业航天', limit_up_count: 13 }] }))
    render(<MarketMainlineCard />)
    expect((await screen.findByTestId('concept-breadth-leaders')).textContent).toContain('商业航天')
  })
  it('缺省视图: 有概念数据时并列展示, 热点概念无需点击即可见', async () => {
    mocks.fetchAPI.mockResolvedValue(baseResp())
    render(<MarketMainlineCard />)

    // 行业与概念同屏
    expect(await screen.findByText('半导体')).toBeTruthy()
    expect(screen.getByText('商业航天')).toBeTruthy()
    // 概念列标题写明独立维度与不可相加
    expect(screen.getByText(/概念 · 独立维度/)).toBeTruthy()
    // 概念行有「概念」维度标记, 行业行没有
    const conceptRow = screen.getByText('商业航天').closest('button') as HTMLElement
    expect(within(conceptRow).getByText('概念')).toBeTruthy()
    const industryRow = screen.getByText('半导体').closest('button') as HTMLElement
    expect(within(industryRow).queryByText('概念')).toBeNull()
  })

  it('概念页只读 concept_groups: 行业榜的名字不得出现在概念页', async () => {
    mocks.fetchAPI.mockResolvedValue(baseResp())
    render(<MarketMainlineCard />)
    await screen.findByText('半导体')

    fireEvent.click(screen.getByRole('tab', { name: /概念/ }))

    expect(await screen.findByText('商业航天')).toBeTruthy()
    expect(screen.queryByText('半导体')).toBeNull()
    expect(screen.queryByText('半导体龙头')).toBeNull()
  })

  it('口径条: 明示不可相加 / 重叠次数 / 时点 / 数据源 / 龙头仅组内排序', async () => {
    mocks.fetchAPI.mockResolvedValue(baseResp())
    render(<MarketMainlineCard />)
    await screen.findByText('商业航天')

    expect(screen.getByText('各概念家数不可相加')).toBeTruthy()
    expect(screen.getByText(/家数合计 17\(重叠 \+5\)/)).toBeTruthy()
    expect(screen.getByText('11-06')).toBeTruthy() // as_of 原样映射, 不是当天
    expect(screen.getByText('tq:get_relation')).toBeTruthy()
    expect(screen.getByText('龙头仅组内排序')).toBeTruthy()
  })

  it('展开概念行: 龙头只是组内排序第一, 并列出该股的其他概念归属', async () => {
    mocks.fetchAPI.mockResolvedValue(baseResp())
    render(<MarketMainlineCard />)
    await screen.findByText('商业航天')

    fireEvent.click(screen.getByText('商业航天'))

    expect(await screen.findByText(/非该股专属驱动/)).toBeTruthy()
    expect(screen.getByText(/龙头同属：卫星互联网、军工电子/)).toBeTruthy()
    expect(screen.getByText(/独占 50%/)).toBeTruthy()
  })

  it('后端没给概念数据: 概念页为空且不冒充, 行业页照常', async () => {
    mocks.fetchAPI.mockResolvedValue(
      baseResp({
        concept_groups: [],
        concept_unranked: [],
        concept_stats: {
          distinct_stocks: 0,
          tagged_stocks: 0,
          untagged_stocks: 21,
          multi_concept_stocks: 0,
          member_count_sum: 0,
          overlap_excess: 0,
          overlap_ratio: 0,
          broad_filtered: 0,
          summable: false,
          min_limitup_for_rank: 3,
          note: '无概念归属数据（涨停池为空或数据源未提供概念字段）',
        },
        data_meta: {
          as_of: null,
          computed_at: '2026-11-09T11:09:12+08:00',
          pool_source: 'tq',
          concept_source: null,
          as_of_note: '涨停池未提供交易日字段，数据时点未知（未猜测为当日）',
        },
      }),
    )
    render(<MarketMainlineCard />)

    // 无概念数据 → 缺省仍是行业(不并列)
    expect(await screen.findByText('半导体')).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: /概念/ }))

    expect(await screen.findByText(/无概念归属数据/)).toBeTruthy()
    expect(screen.getByText(/未以行业数据代替概念/)).toBeTruthy()
    expect(screen.queryByText('半导体')).toBeNull()
    // 时点未知不猜成当天, 并给出原因
    expect(screen.getByText('时点未知')).toBeTruthy()
    expect(screen.getByText(/未猜测为当日/)).toBeTruthy()
    // 概念计数为 0, 不虚报
    expect(screen.getByRole('tab', { name: /概念 0/ })).toBeTruthy()
  })

  it('涨停池为空时: 不渲染任何概念/行业卡片数据, 只说实话', async () => {
    mocks.fetchAPI.mockResolvedValue({
      total_groups: 0,
      ranked_groups: [],
      unranked: [],
      total_limit_ups: 0,
      concept_groups: [],
      concept_unranked: [],
      note: '无涨停池数据(非交易日/数据源不可用)',
    })
    render(<MarketMainlineCard />)

    expect(await screen.findByText(/无涨停池数据/)).toBeTruthy()
    expect(screen.queryByText('商业航天')).toBeNull()
    expect(screen.queryByText('半导体')).toBeNull()
  })

  it('刷新走 /market/mainline 且跳过 GET 缓存', async () => {
    mocks.fetchAPI.mockResolvedValue(baseResp())
    render(<MarketMainlineCard />)
    await screen.findByText('商业航天')
    await waitFor(() => expect(mocks.fetchAPI).toHaveBeenCalled())
    expect(mocks.fetchAPI.mock.calls[0][0]).toBe('/market/mainline')
    expect(mocks.fetchAPI.mock.calls[0][1]).toMatchObject({ cacheMode: 'reload' })
  })
})
