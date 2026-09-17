// @vitest-environment jsdom
//
// 「免费档」面板(2026-09-18) —— owner 运行时调整免费级别的 UI 回归。
//
// 钉四件事:
//  ① 只渲染后端给的真实配置(功能清单来自 catalog, 不硬编码; 默认 pro 专属/默认就给 有区分);
//  ② 勾选"数智决策"再保存 → PUT 的 patch 里 trial_features 含 view_forecast(值=中文名);
//  ③ skill 档位: 改成非内置值 → 写进 skill_tier_overrides; 改回内置值 → **删掉覆盖项**;
//  ④ 非 owner(403) → 整块不渲染(不画假开关), 且不报错干扰页面。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FreeTierSection } from '@/components/settings/FreeTierSection'

const { getMock, updateMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  updateMock: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  freeTierApi: { get: getMock, update: updateMock },
}))

const RESPONSE = {
  config: {
    trial_features: { view_opportunities: '机会', view_l2: 'L2资金', view_dark: '暗盘资金' },
    trial_daily_limit: 3,
    member_watchlist_max: 10,
    member_alert_max: 3,
    skill_tier_overrides: {},
  },
  defaults: {
    trial_features: { view_opportunities: '机会', view_l2: 'L2资金', view_dark: '暗盘资金' },
    trial_daily_limit: 3,
    member_watchlist_max: 10,
    member_alert_max: 3,
    skill_tier_overrides: {},
  },
  catalog: {
    features: [
      { perm: 'view_opportunities', label: '机会', trial: true },
      { perm: 'view_l2', label: 'L2资金', trial: true },
      { perm: 'view_dark', label: '暗盘资金', trial: true },
      { perm: 'view_forecast', label: '数智决策', trial: false },
      { perm: 'view_auction', label: '集合竞价池', trial: false },
    ],
    skills: [
      { name: 'get_stock_quote', builtin_tier: 'free' as const, effective_tier: 'free' as const, overridden: false },
      { name: 'get_auction_data', builtin_tier: 'pro' as const, effective_tier: 'pro' as const, overridden: false },
      { name: 'get_decision_pioneer', builtin_tier: 'pro' as const, effective_tier: 'pro' as const, overridden: false },
    ],
  },
  cache_ttl_seconds: 30,
}

beforeEach(() => {
  getMock.mockReset()
  updateMock.mockReset()
  getMock.mockResolvedValue(RESPONSE)
  updateMock.mockResolvedValue({ config: RESPONSE.config, catalog: RESPONSE.catalog })
})

// 本仓 vitest 未开 globals, RTL 自动 cleanup 不生效 → 显式清理
afterEach(() => cleanup())

describe('免费档面板', () => {
  it('按后端目录渲染功能开关, 并区分"默认就给 / 默认 pro 专属"', async () => {
    render(<FreeTierSection />)

    const forecast = (await screen.findByLabelText('试用-数智决策')) as HTMLInputElement
    const auction = screen.getByLabelText('试用-集合竞价池') as HTMLInputElement
    const opp = screen.getByLabelText('试用-机会') as HTMLInputElement

    // pro 专属默认不勾; 默认就给的功能是勾上的
    expect(forecast.checked).toBe(false)
    expect(auction.checked).toBe(false)
    expect(opp.checked).toBe(true)
    expect(screen.getAllByText('（默认 pro 专属）').length).toBe(2)
  })

  it('勾上数智决策 + 保存 → patch 带 view_forecast(值=中文名)', async () => {
    render(<FreeTierSection />)
    fireEvent.click(await screen.findByLabelText('试用-数智决策'))
    fireEvent.click(screen.getByText('保存'))

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1))
    const patch = updateMock.mock.calls[0][0]
    expect(patch.trial_features.view_forecast).toBe('数智决策')
    expect(patch.trial_features.view_auction).toBeUndefined()
    expect(patch.trial_daily_limit).toBe(3)
    // 保存成功要有明确反馈(30s 热生效), 不能静默
    expect(await screen.findByText(/30s 内全节点生效/)).toBeTruthy()
  })

  it('skill 档位改成 free → 写进覆盖; 改回内置值 → 删掉覆盖项', async () => {
    render(<FreeTierSection />)
    const sel = (await screen.findByLabelText('档位-get_auction_data')) as HTMLSelectElement
    expect(sel.value).toBe('pro') // 未覆盖时显示内置档位

    fireEvent.change(sel, { target: { value: 'free' } })
    fireEvent.click(screen.getByText('保存'))
    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1))
    expect(updateMock.mock.calls[0][0].skill_tier_overrides).toEqual({ get_auction_data: 'free' })

    // 改回内置 pro → 覆盖项应被移除(不是写一条 pro 的冗余覆盖)
    fireEvent.change(sel, { target: { value: 'pro' } })
    fireEvent.click(screen.getByText('保存'))
    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(2))
    expect(updateMock.mock.calls[1][0].skill_tier_overrides).toEqual({})
  })

  it('非 owner(403) → 整块不渲染', async () => {
    const err: any = new Error('Forbidden')
    err.status = 403
    getMock.mockRejectedValue(err)

    const { container } = render(<FreeTierSection />)
    await waitFor(() => expect(getMock).toHaveBeenCalled())
    await waitFor(() => expect(container.textContent).toBe(''))
  })

  it('读取失败(非 403) → 显式报错, 不假装没这功能', async () => {
    getMock.mockRejectedValue(new Error('boom'))
    render(<FreeTierSection />)
    expect(await screen.findByText(/免费档：boom/)).toBeTruthy()
  })
})
