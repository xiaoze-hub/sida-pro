// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn(), role: vi.fn(() => 'owner') }))
vi.mock('@panwatch/api', () => ({ fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a) }))
vi.mock('@/lib/jwt', () => ({ getJwtRole: () => mocks.role() }))

import { MarketPhasePanel } from '@/components/MarketPhasePanel'

const SEGS = {
  available: true,
  total_days: 120,
  segments: [
    { phase: 'repair', label: '修复', start: '2026-08-01', end: '2026-08-20', days: 14,
      avg_height: 4, avg_first_board: 30, avg_ge2: 8, avg_promo: 0.2, avg_seal_rate: 0.6 },
    { phase: 'ignite', label: '启动', start: '2026-08-21', end: '2026-08-25', days: 3,
      avg_height: 5, avg_first_board: 40, avg_ge2: 11, avg_promo: 0.24, avg_seal_rate: 0.64 },
    { phase: 'repair', label: '修复', start: '2026-08-26', end: '2026-09-11', days: 12,
      avg_height: 4, avg_first_board: 28, avg_ge2: 7, avg_promo: 0.19, avg_seal_rate: 0.6 },
  ],
}
const STATS = {
  available: true,
  total_days: 120,
  stats: {
    repair: { count: 9, total_days: 210, max_days: 99, avg_days: 23.3, label: '修复',
      next: { ignite: 0.6, ebb: 0.4 }, next_labels: { 启动: 0.6, 退潮: 0.4 } },
    ignite: { count: 4, total_days: 12, max_days: 5, avg_days: 3, label: '启动',
      next: { rally: 0.5, repair: 0.5 }, next_labels: { 主升: 0.5, 修复: 0.5 } },
  },
  percentiles: { first_board: 42, ge2_count: 38, max_height: 55, promo_rate: 61, completeness: 70 },
}

describe('MarketPhasePanel', () => {
  afterEach(() => cleanup())
  beforeEach(() => mocks.fetchAPI.mockReset())

  it('无历史时给可操作空态(owner 见回填按钮)', async () => {
    mocks.fetchAPI.mockResolvedValue({ available: false, segments: [], total_days: 0 })
    render(<MarketPhasePanel />)
    expect(await screen.findByText('尚无阶段历史')).toBeTruthy()
    expect(screen.getByText('回填历史阶段')).toBeTruthy()
  })

  it('非 owner 不显示回填按钮', async () => {
    mocks.role.mockReturnValue('member')
    mocks.fetchAPI.mockResolvedValue({ available: false, segments: [], total_days: 0 })
    render(<MarketPhasePanel />)
    expect(await screen.findByText('尚无阶段历史')).toBeTruthy()
    expect(screen.queryByText('回填历史阶段')).toBeNull()
    expect(screen.getByText('需管理员在后台触发回填')).toBeTruthy()
  })

  it('有历史时渲染当前阶段/分位徽标/去向/色带', async () => {
    mocks.fetchAPI.mockImplementation((url: string) =>
      Promise.resolve(String(url).includes('/segments') ? SEGS : STATS),
    )
    const { container } = render(<MarketPhasePanel />)
    expect(await screen.findByText('修复 · 第 12 天')).toBeTruthy()
    // 分位徽标: 标签与数值分两个节点, 数值节点是 mono 的 "pNN"
    expect(screen.getByText('p42')).toBeTruthy()
    expect(screen.getByText('p70')).toBeTruthy()
    expect(screen.getByText('首板')).toBeTruthy()
    expect(screen.getByText('梯队完整度')).toBeTruthy()
    // 阶段规律: 段数/时长/去向
    expect(screen.getByText('· 历史 9 段')).toBeTruthy()
    expect(screen.getByText('启动 60%')).toBeTruthy()
    // 色带: 段数 = 3 个色块 + 图例 7 个色块
    expect(container.querySelectorAll('[title*="天 · 均高"]').length).toBe(3)
  })
})
