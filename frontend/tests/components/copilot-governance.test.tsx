// @vitest-environment jsdom
/**
 * 治理看板页面（P3）：三个 tab 能渲染、轨迹能展开、成本未定价显式标注、配额可保存。
 * 口径：缺失一律 `--`，不写 0。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({
  getCopilotRuns: vi.fn(),
  getCopilotRunDetail: vi.fn(),
  getCopilotCost: vi.fn(),
  getCopilotQuotas: vi.fn(),
  putCopilotQuota: vi.fn(),
}))

vi.mock('@/lib/copilot/api', async () => ({
  ...(await vi.importActual<Record<string, unknown>>('@/lib/copilot/api')),
  ...api,
}))

import CopilotGovernancePage from '@/pages/CopilotGovernance'

afterEach(cleanup)

describe('copilot governance', () => {
  beforeEach(() => {
    Object.values(api).forEach((f) => f.mockReset())
    api.getCopilotRuns.mockResolvedValue([
      {
        run_id: 'r1',
        conversation_id: 1,
        recipe: null,
        status: 'ok',
        tool_calls: 2,
        elapsed_ms: 3200,
        tokens: null,
        question: '金健米业竞价怎么样',
        created_at: '2026-10-20 15:00:00',
      },
    ])
    api.getCopilotRunDetail.mockResolvedValue({
      run: {
        run_id: 'r1',
        status: 'ok',
        recipe: null,
        question: '金健米业竞价怎么样',
        tool_calls: 1,
        elapsed_ms: 3200,
        created_at: '2026-10-20 15:00:00',
      },
      steps: [
        { seq: 1, tool: 'get_auction_data', ok: true, elapsed_ms: 210, args: '{"symbol":"600127"}', result_digest: '低开 -1.75%' },
      ],
    })
    api.getCopilotCost.mockResolvedValue({
      days: 7,
      total_tokens: 1200,
      total_calls: 3,
      by_day: [{ date: '2026-10-20', tokens: 1200, calls: 3, cost: null }],
      by_model: [{ model: 'mystery', tokens: 1200, calls: 3, cost: null }],
      by_user: [{ user_id: 'u1', tokens: 1200, calls: 3, cost: null }],
      unpriced_models: ['mystery'],
      scope: 'self',
    })
    api.getCopilotQuotas.mockResolvedValue({
      scope: 'all',
      items: [
        {
          user_id: 'u1',
          daily_runs: 10,
          daily_tokens: null,
          enabled: true,
          used_runs: 3,
          used_tokens: 1200,
        },
      ],
    })
    api.putCopilotQuota.mockResolvedValue({
      user_id: 'u1',
      daily_runs: 20,
      daily_tokens: null,
      enabled: true,
      used_runs: 3,
      used_tokens: 1200,
    })
  })

  it('renders trace list and replays steps', async () => {
    render(<CopilotGovernancePage />)
    const row = await screen.findByText('金健米业竞价怎么样')
    expect(row).toBeTruthy()
    fireEvent.click(row)
    expect(await screen.findByText('get_auction_data')).toBeTruthy()
    expect(screen.getByText(/低开 -1.75%/)).toBeTruthy()
  })

  it('marks unpriced models and shows -- instead of a fake cost', async () => {
    render(<CopilotGovernancePage />)
    fireEvent.click(screen.getByRole('button', { name: '成本' }))
    expect(await screen.findByText(/未配置单价的模型/)).toBeTruthy()
    // 两处 `--`（按模型/按用户），只要求存在且不出现伪造金额
    expect(screen.getAllByText('--').length).toBeGreaterThan(0)
    expect(screen.queryByText(/¥/)).toBeNull()
  })

  it('saves quota via PUT', async () => {
    render(<CopilotGovernancePage />)
    fireEvent.click(screen.getByRole('button', { name: '配额' }))
    const save = await screen.findByRole('button', { name: '保存' })
    fireEvent.click(save)
    await waitFor(() => expect(api.putCopilotQuota).toHaveBeenCalled())
    expect(api.putCopilotQuota.mock.calls[0][0]).toBe('u1')
  })
})
