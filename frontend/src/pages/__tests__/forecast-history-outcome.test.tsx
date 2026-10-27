// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * Forecast 页历史表「到期对照」列整页回归(2026-10-24)。
 *
 * 契约(docs/design/20261023_04 §1.6): `no_data`(到期但取不到行情)必须显示
 * 灰虚线「无数据」, **不得**显示 `✗`;`pending` 显示「待验证」;只有 hit/miss 出 ✓/✗。
 * 旧实现只分 hit / !hit, no_data 直接落进 ✗ —— 缺失数据被当成"未命中"。
 */

const api = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  getToken: () => 'test-token',
  stocksApi: { list: vi.fn() },
}))

vi.mock('@sida/api', () => ({
  fetchAPI: api.fetchAPI,
  getToken: api.getToken,
  stocksApi: api.stocksApi,
}))

vi.mock('@sida/base-ui/components/ui/toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}))

const history = [
  { id: 1, symbol: '600000', last_close: 10, last_date: '2026-10-20', pred_days: 3, direction: 'up', expected_pct: 1.8, prediction: [10.2], action: '观望', confidence: '中', target_price: 10.2, stop_loss: 9.8, summary: '', sentiment_adj: 0, created_at: '2026-10-20 09:00', outcome_status: 'no_data' },
  { id: 2, symbol: '000001', last_close: 12, last_date: '2026-10-18', pred_days: 3, direction: 'up', expected_pct: 1.2, prediction: [12.1], action: '观望', confidence: '中', target_price: 12.1, stop_loss: 11.7, summary: '', sentiment_adj: 0, created_at: '2026-10-18 09:00', outcome_status: 'miss', outcome_return_pct: -2.1 },
  { id: 3, symbol: '002001', last_close: 8, last_date: '2026-10-22', pred_days: 3, direction: 'down', expected_pct: -1.1, prediction: [7.9], action: '观望', confidence: '低', target_price: 7.9, stop_loss: 8.2, summary: '', sentiment_adj: 0, created_at: '2026-10-22 09:00', outcome_status: 'pending' },
]

beforeEach(() => {
  api.stocksApi.list.mockResolvedValue([])
  api.fetchAPI.mockImplementation(async (url: string) => {
    if (url.startsWith('/forecast/history')) return { items: history }
    if (url === '/forecast/weights') throw new Error('HTTP 502') // 引擎未起 → 权重不可用
    if (url === '/forecast/health') return { status: 'ok' }
    return {}
  })
})

afterEach(() => {
  cleanup()
  api.fetchAPI.mockReset()
})

describe('Forecast 历史表到期对照列', () => {
  it('no_data 行显示「无数据」而不是 ✗；pending 显示「待验证」', async () => {
    const { default: ForecastPage } = await import('../Forecast')
    const { container } = render(<ForecastPage />)

    await waitFor(() => expect(screen.getByText('到期对照')).toBeTruthy())

    // 取不到行情那条: 中性「无数据」印章
    expect(screen.getAllByText('无数据').length).toBeGreaterThanOrEqual(1)
    // 未到期那条
    expect(screen.getByText('待验证')).toBeTruthy()

    const rows = Array.from(container.querySelectorAll('tbody tr'))
    const noDataRow = rows.find(r => r.textContent?.includes('600000'))
    expect(noDataRow).toBeTruthy()
    expect(noDataRow!.textContent).toContain('无数据')
    expect(noDataRow!.textContent).not.toContain('✗')
    expect(noDataRow!.textContent).not.toContain('✓')

    // miss 那条仍是 ✗(唯一允许出"未命中"结论的状态)
    const missRow = rows.find(r => r.textContent?.includes('000001'))
    expect(missRow!.textContent).toContain('✗')
  })

  it('列头 title 报出分母: 无数据/待验证不计入', async () => {
    const { default: ForecastPage } = await import('../Forecast')
    render(<ForecastPage />)
    await waitFor(() => expect(screen.getByText('到期对照')).toBeTruthy())
    const th = screen.getByText('到期对照')
    const title = th.getAttribute('title') || ''
    expect(title).toContain('已判定 1 条')
    expect(title).toContain('无数据 1 条')
    expect(title).toContain('待验证 1 条')
    expect(title).toContain('不计入分母')
  })
})
