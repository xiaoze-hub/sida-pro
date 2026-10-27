// @vitest-environment jsdom
/**
 * 治理条：预算上限/已用、白名单条数、Frozen Ring。
 *
 * 口径红线：
 *  - 缺上限显示 `--`，不显示 0；
 *  - 白名单 0 条说「未限定」，不能说「允许所有」；
 *  - 读取失败要透出原文，不能静默空着让人以为"没有约束"。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

// 组件内部走 @sida/api 的 fetchAPI, 这里 mock 那一层(url 分发), 不 mock 组件模块
const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', async () => ({
  ...(await vi.importActual<Record<string, unknown>>('@sida/api')),
  ...api,
}))

import { GovernanceStrip, budgetText, whitelistText } from '@/components/copilot/GovernanceStrip'
import type { RunGovernance } from '@/components/copilot/GovernanceStrip'

const FULL: RunGovernance = {
  run_id: 'r1',
  status: 'ok',
  recipe: 'deep_diagnosis',
  budget: { max: 11, used: 3, remaining: 8 },
  whitelist: { count: 11, tools: Array.from({ length: 11 }, (_, i) => `t${i}`) },
  frozen_ring: { guards: ['a', 'b', 'c', 'd'], note: '以上四条属 Frozen Ring：不得覆盖' },
  limits: { max_tool_calls: 11, recipe_tools_declared: 11 },
}

beforeEach(() => {
  api.fetchAPI.mockReset()
  api.fetchAPI.mockResolvedValue(FULL)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('budgetText / whitelistText', () => {
  it('预算显示 已用/上限', () => {
    expect(budgetText(FULL)).toBe('3 / 11')
  })

  it('缺上限显示 --，不显示 0', () => {
    expect(budgetText({ ...FULL, budget: { max: null, used: 5, remaining: null } })).toBe('5 / --')
    expect(budgetText(null)).toBe('--')
  })

  it('白名单 0 条 = 未限定（不是允许所有）', () => {
    expect(whitelistText({ ...FULL, whitelist: { count: 0, tools: [] } })).toBe('未限定')
    expect(whitelistText(FULL)).toBe('11 个工具')
    expect(whitelistText(null)).toBe('--')
  })
})

describe('GovernanceStrip', () => {
  it('展示预算/白名单/Frozen Ring 条数，并标明只读', async () => {
    render(<GovernanceStrip runId="r1" />)
    await waitFor(() => expect(screen.getByText('治理')).toBeTruthy())
    expect(screen.getByText('3 / 11')).toBeTruthy()
    expect(screen.getByText('11 个工具')).toBeTruthy()
    expect(screen.getByText('4')).toBeTruthy()
    // 文案已改为「拦截记录可查」（治理条只展示, 拦截由后端执行）
    expect(screen.getByText(/拦截记录可查/)).toBeTruthy()
  })

  it('没有 run_id 时不渲染也不请求', () => {
    const { container } = render(<GovernanceStrip runId={null} />)
    expect(container.firstChild).toBeNull()
    expect(api.fetchAPI).not.toHaveBeenCalled()
  })

  it('读取失败透出服务端原文', async () => {
    api.fetchAPI.mockRejectedValue(new Error('任务不存在'))
    render(<GovernanceStrip runId="r1" />)
    await waitFor(() => expect(screen.getByText(/读取失败/)).toBeTruthy())
    expect(screen.getByText(/任务不存在/)).toBeTruthy()
  })
})
