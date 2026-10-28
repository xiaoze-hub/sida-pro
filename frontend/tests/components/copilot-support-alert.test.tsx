// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const mocks = vi.hoisted(() => ({
  list: vi.fn(), create: vi.fn(), fetch: vi.fn(), toast: vi.fn(),
}))
vi.mock('@sida/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('@sida/api')>(),
  stocksApi: { list: mocks.list, create: mocks.create },
  fetchAPI: mocks.fetch,
}))
vi.mock('@sida/base-ui/components/ui/toast', () => ({ useToast: () => ({ toast: mocks.toast }) }))
import PriceAlertsPage from '@/pages/PriceAlerts'

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('参谋支撑破位提醒', () => {
  it('真实表单提交目标股票、跌破方向与价位', async () => {
    mocks.list.mockResolvedValue([{ id: 7, symbol: '600111', name: '北方稀土', market: 'CN' }])
    mocks.fetch.mockResolvedValue([])
    render(<MemoryRouter initialEntries={['/notifications?tab=alerts&symbol=600111&price=32.8&intent=support_break']}><PriceAlertsPage /></MemoryRouter>)
    await screen.findByDisplayValue('32.8')
    fireEvent.click(screen.getByRole('button', { name: '保存规则' }))
    await waitFor(() => {
      const call = mocks.fetch.mock.calls.find(([url, options]) => url === '/price-alerts' && options?.method === 'POST')
      expect(call).toBeTruthy()
      expect(JSON.parse(call![1].body)).toMatchObject({ stock_id: 7, condition_group: { items: [{ type: 'price', op: '<', value: 32.8 }] } })
    })
  })

  it('目标不存在时不创建自选股、不选其他股票', async () => {
    mocks.list.mockResolvedValue([{ id: 8, symbol: '000001', name: '其他股票', market: 'CN' }])
    mocks.fetch.mockResolvedValue([])
    render(<MemoryRouter initialEntries={['/notifications?tab=alerts&symbol=600111&price=32.8&intent=support_break']}><PriceAlertsPage /></MemoryRouter>)
    await waitFor(() => expect(mocks.toast).toHaveBeenCalled())
    expect(mocks.create).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '保存规则' })).toBeNull()
    expect(mocks.fetch.mock.calls.some(([url, options]) => url === '/price-alerts' && options?.method === 'POST')).toBe(false)
  })
})
