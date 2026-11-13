// @vitest-environment jsdom
//
// 2026-11-13: 历史无归属经验参考 / system 桶独立口径上屏。
// 钉死三件事:
//   1. 旧数据只以**全局聚合**出现(无个股/明细/用户标识), 且明确"非个人成绩";
//   2. 真实分母与重复键风险必须显示, 样本不足不参评;
//   3. 评分阻断/请求失败时显式说明, 不编造任何百分比。
import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  fail: null as string | null,
  legacy: null as unknown,
  system: null as unknown,
}))

vi.mock('@sida/api', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    fetchAPI: (path: string) => {
      if (api.fail && path.startsWith('/learning/reference')) {
        return Promise.reject(new Error(api.fail))
      }
      if (path === '/learning/reference/legacy') return Promise.resolve(api.legacy)
      if (path === '/learning/reference/system') return Promise.resolve(api.system)
      return Promise.resolve({})
    },
  }
})

import { LearningReferenceSection } from '@/components/profile/LearningReferenceSection'

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={client}>
      <LearningReferenceSection />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  api.fail = null
  api.legacy = {
    scope: 'legacy_unowned',
    labels: ['历史无归属', '未验证', '仅经验参考', '非个人成绩'],
    scoring_blocked: false,
    trainable: false,
    auto_weighting: false,
    sample: {
      evaluated_rows: 44967,
      directional_sample: 40100,
      excluded_neutral_rows: 4867,
      pending_rows: 120,
      min_n: 30,
    },
    duplicate_risk: { key_groups_with_duplicates: 37, rows_in_duplicate_groups: 81 },
    excluded: { detached_account_rows: 4, ghost_user_rows: 2 },
    overall: { hit_count: 20500, total: 40063, raw_total: 40100, hit_rate: 51.2, basis: 'deduped_first_row_per_key' },
    agents: [
      { agent: 'premarket_outlook', hit: 900, total: 1600, hit_rate: 56.3, avg_return_pct: 0.8, qualified: true },
      { agent: 'rare', hit: 1, total: 1, hit_rate: null, avg_return_pct: null, qualified: false, small_sample: true },
    ],
    note: '历史无归属记录(加列前存量): 归属不可证明, 不纳入任何人的命中率',
  }
  api.system = {
    scope: 'system',
    labels: ['系统运行留档', '未验证', '仅经验参考', '非个人成绩'],
    scoring_blocked: false,
    independent_of_personal: true,
    sample: { evaluated_rows: 12, directional_sample: 10, excluded_neutral_rows: 2, pending_rows: 3 },
    duplicate_risk: { key_groups_with_duplicates: 0, rows_in_duplicate_groups: 0 },
    overall: { hit_count: 6, total: 10, raw_total: 10, hit_rate: 60.0, basis: 'deduped_first_row_per_key' },
    agents: [{ agent: 'premarket_outlook', hit: 6, total: 10, hit_rate: 60.0, avg_return_pct: 0.3, qualified: true }],
    note: 'system 桶(无会话系统调度运行): 与个人成绩独立',
  }
})

afterEach(() => {
  cleanup()
})

describe('历史经验参考 / system 桶独立口径', () => {
  it('旧数据只以全局汇总上屏: 标签 + 真实分母 + 重复风险 + 样本不足', async () => {
    mount()
    const legacy = await screen.findByTestId('profile-history-reference')
    const labels = await within(legacy).findByTestId('profile-history-reference-labels')
    expect(labels.textContent).toContain('历史无归属')
    expect(labels.textContent).toContain('未验证')
    expect(labels.textContent).toContain('非个人成绩')
    const sample = (await within(legacy).findByTestId('profile-history-reference-sample')).textContent || ''
    expect(sample).toContain('44967')
    expect(sample).toContain('真实分母')
    expect(sample).toContain('40063')
    expect(sample).toContain('重复键组')
    expect(sample).toContain('37')
    const excluded = (await within(legacy).findByTestId('profile-history-reference-excluded')).textContent || ''
    expect(excluded).toContain('4')
    expect(excluded).toContain('悬空 user_id')
    expect(within(legacy).getByText(/来源时点不可证/).textContent).toContain('不训练')
    expect((await within(legacy).findByTestId('reference-agent-row-premarket_outlook')).textContent).toContain('56.3%')
    expect((await within(legacy).findByTestId('reference-agent-row-rare')).textContent).toContain('样本不足')
  })

  it('不渲染个股/用户标识, 即使响应里夹带了这些字段', async () => {
    // 后端契约不返回这些字段; 万一上游回归, 界面也不得把它们铺出来。
    ;(api.legacy as Record<string, unknown>).stock_symbol = '600519'
    ;(api.legacy as Record<string, unknown>).user_id = 'u-secret'
    ;(api.legacy as Record<string, unknown>).agents = [
      ...((api.legacy as { agents: unknown[] }).agents),
    ]
    mount()
    const section = await screen.findByTestId('profile-learning-reference')
    expect(section.textContent).not.toContain('600519')
    expect(section.textContent).not.toContain('u-secret')
  })

  it('system 桶独立展示, 明确不并入个人成绩', async () => {
    mount()
    const system = await screen.findByTestId('profile-system-reference')
    const block = within(system)
    expect(block.getByText(/不并入任何账号/)).toBeTruthy()
    expect((await block.findByTestId('reference-agent-row-premarket_outlook')).textContent).toContain('60%')
    expect((await block.findByTestId('profile-system-reference-sample')).textContent).toContain('待评估')
  })

  it('评分阻断: 不显示任何数字/百分比, 只说明原因', async () => {
    api.legacy = {
      scope: 'legacy_unowned',
      scoring_blocked: true,
      block_reason: '四条后验计分唯一索引不可信',
      note: '经验参考已阻断',
    }
    mount()
    const legacy = await screen.findByTestId('profile-history-reference')
    expect((await within(legacy).findByTestId('profile-history-reference-blocked')).textContent).toContain('评分阻断')
    expect(legacy.textContent).not.toContain('%')
    expect(within(legacy).queryByTestId('profile-history-reference-sample')).toBeNull()
  })

  it('请求失败: 显式错误, 不铺空表也不冒充 0 样本', async () => {
    api.fail = '参考接口 503'
    mount()
    const legacy = await screen.findByTestId('profile-history-reference-error')
    expect(legacy.textContent).toContain('加载失败')
    expect(legacy.textContent).toContain('503')
  })
})
