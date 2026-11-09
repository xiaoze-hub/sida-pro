// @vitest-environment jsdom
/**
 * 首页「市场主线」卡数据时点/缓存时效/概念空态契约(2026-11-09 复审修复)。
 *
 * 复审发现三个真问题:
 *  ① 行业视图与空态只显示 `updatedAt.toLocaleTimeString()`(本次拉取墙钟),
 *     后端返回的 data_meta.as_of / stale / age_s 一个都没用 → 用户不知道这榜是哪天的、是不是旧快照;
 *  ② 概念榜为空时一律写"暂无概念归属数据", 即使后端明确给了 concept_unranked
 *     (有概念、只是没达到 ≥3 只涨停门槛) → 把"未达门槛"说成"没有归属";
 *  ③ 概念不可用(数据源没给概念字段)时也不引用后端 concept_note, 读者会误以为是"今日无概念"。
 *
 * 本文件按真实 /api/market/mainline 契约(见 src/web/api/market_mainline.py 与
 * src/core/market_mainline.py 的 aggregate_mainline 输出)构造载荷, 逐条锁死。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@sida/api', () => ({ fetchAPI: vi.fn() }))

import { fetchAPI } from '@sida/api'
import MarketMainlineCard from '@sida/biz-ui/components/MarketMainlineCard'

const mockFetch = vi.mocked(fetchAPI)

const industryGroup = (name: string, limitUp = 5) => ({
  name,
  dimension: 'industry',
  limit_up_count: limitUp,
  ge2_count: 2,
  max_boards: 4,
  boards_sum: 8,
  rungs: 2,
  score: 80,
  leader: { code: '600111', name: '北方稀土', days: 4, amount: 1.2e9 },
  constituents: [{ code: '600111', name: '北方稀土', days: 4, amount: 1.2e9 }],
})

const conceptGroup = (name: string, limitUp = 1) => ({
  ...industryGroup(name, limitUp),
  dimension: 'concept',
  score: null,
  leader_shared_concepts: [],
  leader_basis: '本概念内连板数优先、成交额次之排序第一；非该股专属驱动',
  exclusive_ratio: 1,
})

/** 真实后端空池/正常池都必带的 concept_stats 骨架 */
const conceptStats = (over: Record<string, unknown> = {}) => ({
  distinct_stocks: 0,
  tagged_stocks: 0,
  untagged_stocks: 10,
  multi_concept_stocks: 0,
  member_count_sum: 0,
  overlap_excess: 0,
  overlap_ratio: 0,
  broad_filtered: 0,
  summable: false,
  min_limitup_for_rank: 3,
  note: '',
  ...over,
})

const baseMeta = {
  as_of: '2026-11-06',
  computed_at: '2026-11-09T09:31:05+08:00',
  pool_source: 'tq',
  concept_source: 'tq:get_relation',
  concept_source_note: '',
  concept_status: 'available',
  concept_note: '',
  as_of_note: '',
}

const payload = (over: Record<string, unknown> = {}) => ({
  total_groups: 1,
  total_limit_ups: 10,
  min_limitup_for_rank: 3,
  ranked_groups: [industryGroup('稀土永磁')],
  unranked: [],
  filter_stats: { broad_filtered: 0, below_min: 0, ranked: 1 },
  dimension: 'industry',
  concept_groups: [],
  concept_unranked: [],
  concept_status: 'available',
  concept_note: '',
  concept_stats: conceptStats({ distinct_stocks: 3, tagged_stocks: 3, untagged_stocks: 7 }),
  data_meta: { ...baseMeta },
  note: '',
  cache_ts: 1762738265,
  stale: false,
  age_s: 42.6,
  ...over,
})

/** 渲染并等待首拉落地(首帧是「正在加载市场主线…」) */
async function renderCard(data: Record<string, unknown>) {
  mockFetch.mockResolvedValueOnce(data as never)
  render(<MarketMainlineCard />)
  await waitFor(() => expect(screen.queryByText('正在加载市场主线…')).toBeNull())
  return screen.getByTestId('mainline-meta')
}

afterEach(() => {
  cleanup()
  mockFetch.mockReset()
})

describe('主线卡: 数据日与缓存时效(行业视图)', () => {
  it('行业视图消费 data_meta.as_of, 并标出旧数据与年龄', async () => {
    const meta = await renderCard(payload({ stale: true, age_s: 742.4 }))
    expect(meta.textContent).toContain('数据日')
    expect(meta.textContent).toContain('11-06')
    expect(meta.textContent).toContain('旧数据')
    expect(meta.textContent).toContain('12 分钟前')
    expect(meta.textContent).toContain('tq')
  })

  it('as_of 缺失 → 明写时点未知并引用后端 as_of_note, 不编日期', async () => {
    const asOfNote = '涨停池未提供交易日字段，数据时点未知（未猜测为当日）'
    const meta = await renderCard(
      payload({
        data_meta: { ...baseMeta, as_of: null, as_of_note: asOfNote },
        stale: false,
        age_s: 12,
      }),
    )
    expect(meta.textContent).toContain('时点未知')
    expect(meta.textContent).toContain('未猜测为当日')
    // 只有拉取墙钟是不够的: 必须同时出现缓存年龄这一栏
    expect(meta.textContent).toContain('缓存')
  })

  it('stale=true 且 age_s 缺失 → 旧数据 + 时效未知(不假装新鲜)', async () => {
    const meta = await renderCard(payload({ stale: true, age_s: null }))
    expect(meta.textContent).toContain('旧数据')
    expect(meta.textContent).toContain('时效未知')
  })

  it('stale 完全缺失时也不写「实时/最新」', async () => {
    const meta = await renderCard(payload({ stale: undefined, age_s: undefined }))
    expect(meta.textContent).toContain('时效未知')
  })
})

describe('主线卡: 空态也必须有数据日与缓存时效', () => {
  it('无涨停池空态显示 as_of / 旧数据 / 诚实原因', async () => {
    const meta = await renderCard(
      payload({
        total_groups: 0,
        total_limit_ups: 0,
        ranked_groups: [],
        unranked: [],
        filter_stats: { broad_filtered: 0, below_min: 0, ranked: 0 },
        concept_groups: [],
        concept_unranked: [],
        concept_status: 'unavailable',
        concept_note: '无涨停池数据（非交易日/数据源不可用），概念维度无输入，不代表今日没有概念题材。',
        concept_stats: conceptStats({ untagged_stocks: 0 }),
        data_meta: { ...baseMeta, concept_source: null, concept_status: 'unavailable', concept_note: '无涨停池数据（非交易日/数据源不可用），概念维度无输入，不代表今日没有概念题材。', as_of_note: '' },
        note: '无涨停池数据(非交易日/数据源不可用)',
        stale: true,
        age_s: 400,
      }),
    )
    expect(meta.textContent).toContain('11-06')
    expect(meta.textContent).toContain('旧数据')
    expect(screen.getByTestId('mainline-empty').textContent).toContain('无涨停池数据')
  })
})

describe('主线卡: 概念空榜区分「未入榜」与「无归属」', () => {
  it('只有未入榜概念时不说"暂无概念归属数据"(并列列 + 概念单列)', async () => {
    await renderCard(
      payload({
        concept_groups: [],
        concept_unranked: [conceptGroup('稀土永磁'), conceptGroup('国企改革')],
        concept_status: 'available',
        concept_stats: conceptStats({ distinct_stocks: 3, tagged_stocks: 3, untagged_stocks: 7 }),
      }),
    )

    // 默认 = 并列视图: 概念列不能写"暂无概念归属数据"
    expect(screen.queryByText(/暂无概念归属数据/)).toBeNull()
    const bothReason = screen.getByTestId('concept-empty-reason')
    expect(bothReason.textContent).toContain('未达')
    expect(bothReason.textContent).toContain('2 个')

    // 概念单列: 同样说清"有概念、只是没到门槛", 并列出未入榜名字
    fireEvent.click(screen.getByRole('tab', { name: /概念/ }))
    const conceptReason = screen.getByTestId('concept-empty-reason')
    expect(conceptReason.textContent).toContain('未达')
    expect(conceptReason.textContent).not.toContain('暂无概念归属数据')
    expect(screen.getByText('稀土永磁')).toBeTruthy()
    expect(screen.getByText('国企改革')).toBeTruthy()
  })

  it('概念源未提供字段时引用 concept_note, 明说"不等于今日无概念"', async () => {
    const note = '当前涨停池来源(tq)未提供概念字段，概念维度为空——这不等于「今日没有概念题材」；本卡片不拿行业/题材冒充概念。'
    await renderCard(
      payload({
        concept_groups: [],
        concept_unranked: [],
        concept_status: 'unavailable',
        concept_note: note,
        concept_stats: conceptStats({ distinct_stocks: 0, tagged_stocks: 0, untagged_stocks: 10 }),
      }),
    )

    // 行业视图也要给出概念不可用的可见提示(默认视图不会自己切走)
    const statusNote = screen.getByTestId('concept-status-note')
    expect(statusNote.textContent).toContain('未提供概念字段')
    expect(statusNote.textContent).toContain('不等于')

    fireEvent.click(screen.getByRole('tab', { name: /概念/ }))
    const reason = screen.getByTestId('concept-empty-reason')
    expect(reason.textContent).toContain('未提供概念字段')
    expect(reason.textContent).not.toContain('暂无概念归属数据')
  })
})
