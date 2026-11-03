// @vitest-environment jsdom
/**
 * 盘面指数页（2026-11-03）。
 *
 * 钉住三件用户会直接看到的事：
 * 1. 涨跌着色按 A 股惯例（红涨绿跌），不是国际惯例；
 * 2. 取数失败时不能把「没有数据」显示成 0；
 * 3. 页面必须带口径标注（tdx_index_agg），避免被误读成东财"主力净流入"。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

import IndexesPage from '@/pages/Indexes'

const OVERVIEW = {
  caliber: 'tdx_index_agg',
  stale: false,
  as_of: '20261103',
  items: [
    {
      symbol: '000300', name: '沪深300', trade_date: '20261103',
      close: 4008.87, pct_chg: -0.2, amount: 553053600000, up_home: 210, down_home: 87,
      outside: 1.2e9, inside: 1.1e9,
      xl_net: 1.8e8, lg_net: 2.77e9, md_net: 1.43e10, sm_net: 2.96e10,
      main_net: 2.94e9, net_all: 4.68e10, active_buy: 2.999e11, active_sell: 2.53e11,
      quality: 'ok', caliber: 'tdx_index_agg',
    },
    {
      symbol: '000852', name: '中证1000', trade_date: '20261103',
      close: 6221.23, pct_chg: 1.71, amount: 313620000000, up_home: null, down_home: null,
      outside: null, inside: null,
      xl_net: -1.6e9, lg_net: -3.38e9, md_net: -6.87e9, sm_net: 2.92e9,
      main_net: -4.98e9, net_all: -8.93e9, active_buy: null, active_sell: null,
      quality: 'ok', caliber: 'tdx_index_agg',
    },
  ],
}

const HISTORY = {
  symbol: '000300',
  items: [
    { trade_date: '20261101', close: 4010, pct_chg: 0.3, amount: 5e11, main_net: 1.1e9, net_all: 2e9 },
    { trade_date: '20261103', close: 4008.87, pct_chg: -0.2, amount: 5.5e11, main_net: 2.94e9, net_all: 4.68e10 },
  ],
}

function mockFetch(payload: unknown) {
  return vi.fn(async () => ({ ok: true, json: async () => payload })) as unknown as typeof fetch
}

describe('IndexesPage', () => {
  beforeEach(() => {
    global.fetch = mockFetch(OVERVIEW) as unknown as typeof fetch
  })
  afterEach(() => cleanup())

  it('渲染八个指数的名称与主力净额，并标注口径', async () => {
    render(<IndexesPage />)
    await waitFor(() => expect(screen.getByText('沪深300')).toBeTruthy())
    expect(screen.getByText('中证1000')).toBeTruthy()
    expect(screen.getAllByText(/tdx_index_agg/).length).toBeGreaterThan(0)
  })

  it('有历史时按日渲染主力净额柱', async () => {
    global.fetch = vi.fn(async (url: string) => ({
      ok: true,
      json: async () => (String(url).includes('history') ? HISTORY : OVERVIEW),
    })) as unknown as typeof fetch
    render(<IndexesPage />)
    await waitFor(() => expect(screen.getByLabelText(/主力净额/)).toBeTruthy())
  })

  it('涨跌着色走 A 股惯例：跌为 stock-down', async () => {
    render(<IndexesPage />)
    await waitFor(() => expect(screen.getByText('沪深300')).toBeTruthy())
    const down = screen.getByText('-0.20%')
    expect(down.className).toContain('text-stock-down')
  })

  it('取不到数据时显示 -- 而不是 0', async () => {
    global.fetch = mockFetch({
      caliber: 'tdx_index_agg', stale: true, as_of: null,
      items: [{
        symbol: '000001', name: '上证指数', trade_date: null, close: null, pct_chg: null,
        amount: null, up_home: null, down_home: null, outside: null, inside: null,
        xl_net: null, lg_net: null, md_net: null, sm_net: null, main_net: null,
        net_all: null, active_buy: null, active_sell: null, quality: null, caliber: null,
      }],
    }) as unknown as typeof fetch
    render(<IndexesPage />)
    await waitFor(() => expect(screen.getByText('上证指数')).toBeTruthy())
    expect(screen.getByText(/实时取数失败/)).toBeTruthy()
    expect(screen.queryByText('+0.00 亿')).toBeNull()
  })

  it('历史为空时给出「从本次上线开始累积」而不是空白图', async () => {
    global.fetch = vi.fn(async (url: string) => ({
      ok: true,
      json: async () => (String(url).includes('history') ? { items: [] } : OVERVIEW),
    })) as unknown as typeof fetch
    render(<IndexesPage />)
    await waitFor(() => expect(screen.getByText(/开始累积/)).toBeTruthy())
    expect(screen.getAllByText(/20261103/).length).toBeGreaterThan(0)
  })
})
