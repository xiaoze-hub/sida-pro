// @vitest-environment jsdom
/** 治理拦截页：只报已发生的事实；空态不能暗示"不会拦"。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', async () => ({
  ...(await vi.importActual<Record<string, unknown>>('@panwatch/api')),
  ...api,
}))

import GovernancePage, { actionLabel, detailFacts } from '@/pages/copilot/GovernancePage'

beforeEach(() => {
  api.fetchAPI.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('detailFacts', () => {
  it('拆出 run id 与其余事实', () => {
    const { runId, parts } = detailFacts('run=abc123 used=12 limit=11')
    expect(runId).toBe('abc123')
    expect(parts).toEqual(['used=12', 'limit=11'])
  })

  it('没有 run 时 runId 为 null，其余原样保留', () => {
    const { runId, parts } = detailFacts('并发上限 2, 当前 3')
    expect(runId).toBeNull()
    expect(parts.join(' ')).toContain('并发上限')
  })
})

describe('actionLabel', () => {
  it('认识两种拦截，未知动作原样显示', () => {
    expect(actionLabel('copilot_tool_budget_exceeded')).toBe('工具调用预算超限')
    expect(actionLabel('copilot_concurrency_exceeded')).toBe('并发上限')
    expect(actionLabel('weird_action')).toBe('weird_action')
  })
})

describe('GovernancePage', () => {
  it('列出拦截记录并可跳到证据页', async () => {
    api.fetchAPI.mockResolvedValue({
      count: 1,
      items: [
        { action: 'copilot_tool_budget_exceeded', detail: 'run=abc123 used=12 limit=11', created_at: '2026-10-26 10:00:00' },
      ],
    })
    render(<GovernancePage />)
    await waitFor(() => expect(screen.getByText('工具调用预算超限')).toBeTruthy())
    expect(screen.getByText('used=12 limit=11')).toBeTruthy()
    const link = screen.getByRole('link', { name: /abc123/i })
    expect(link.getAttribute('href')).toBe('/copilot/runs/abc123')
  })

  it('空态明写"没被拦过，不代表不会拦"', async () => {
    api.fetchAPI.mockResolvedValue({ count: 0, items: [] })
    render(<GovernancePage />)
    await waitFor(() => expect(screen.getByText('没有拦截记录')).toBeTruthy())
    expect(screen.getByText(/不代表不会拦/)).toBeTruthy()
  })

  it('读取失败透出服务端原文', async () => {
    api.fetchAPI.mockRejectedValue(new Error('未登录'))
    render(<GovernancePage />)
    await waitFor(() => expect(screen.getByText(/读取失败/)).toBeTruthy())
    expect(screen.getByText(/未登录/)).toBeTruthy()
  })
})
