// @vitest-environment jsdom
/**
 * §6.2「交割单标 K 线」前端回归。
 *
 * 钉两件事:
 *  ① `tradesToMarkers` 纯映射 —— 买/卖方向、文案里的价×量、缺值显示 `--`(不补 0)、
 *    畸形记录(没有 datetime)整条丢弃;
 *  ② 页面接线 —— `/shadow/trades` 的成交被切成 marker 传给 K 线, 且**只传当前选中标的**的,
 *    没数据时给"没数据 + 下一步"而非空图。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ShadowAccount, { tradesToMarkers } from '@/pages/ShadowAccount'

const { fetchAPIMock, chartProps } = vi.hoisted(() => ({
  fetchAPIMock: vi.fn(),
  chartProps: [] as any[],
}))

vi.mock('@panwatch/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@panwatch/api')>()
  return { ...actual, fetchAPI: fetchAPIMock }
})

vi.mock('@panwatch/biz-ui/components/KlineChart', () => ({
  default: (p: any) => {
    chartProps.push(p)
    return (
      <div data-testid="kline">
        {`sym=${p.symbol};markers=${(p.tradeMarkers || []).map((m: any) => `${m.date}/${m.side}`).join(',')}`}
      </div>
    )
  },
}))

const TRADES = {
  saved: true,
  symbols: ['600519.SH', '002361.SZ'],
  trades: [
    { datetime: '2026-01-05 09:35:00', symbol: '600519.SH', name: '贵州茅台', side: 'buy', quantity: 100, price: 1500.5, amount: 150050, market: 'china_a' },
    { datetime: '2026-01-07 14:20:00', symbol: '600519.SH', name: '贵州茅台', side: 'sell', quantity: 100, price: 1580, amount: 158000, market: 'china_a' },
    { datetime: '2026-01-06 10:00:00', symbol: '002361.SZ', name: '神剑股份', side: 'buy', quantity: 200, price: 6.5, amount: 1300, market: 'china_a' },
  ],
  total: 3,
  capped: false,
  note: '',
}

// 本仓 vitest 未开 `globals`, RTL 的自动 cleanup 不生效 —— 不显式清理会让上个用例的
// DOM 残留到下个用例(本次实测: "无明细 → 不该有图" 断言被上个用例的图命中)。
afterEach(() => cleanup())

describe('tradesToMarkers(纯函数)', () => {
  it('买/卖方向与文案(价×量)', () => {
    const out = tradesToMarkers([
      { ...TRADES.trades[0] } as any,
      { ...TRADES.trades[1] } as any,
    ])
    expect(out).toEqual([
      { date: '2026-01-05', side: 'buy', text: '买1500.5×100' },
      { date: '2026-01-07', side: 'sell', text: '卖1580×100' },
    ])
  })

  it('缺价/缺量显示 --, 不补 0', () => {
    const out = tradesToMarkers([
      { datetime: '2026-01-05 09:35:00', symbol: 'X', name: null, side: 'buy', quantity: null, price: null, amount: null, market: null } as any,
    ])
    expect(out[0].text).toBe('买--×--')
  })

  it('畸形记录(无 datetime / 太短)整条丢弃', () => {
    const out = tradesToMarkers([
      { datetime: '', symbol: 'X', side: 'buy', quantity: 1, price: 1 } as any,
      { datetime: '2026-01', symbol: 'X', side: 'buy', quantity: 1, price: 1 } as any,
      null as any,
    ])
    expect(out).toEqual([])
  })
})

describe('页面接线', () => {
  beforeEach(() => {
    chartProps.length = 0
    fetchAPIMock.mockReset()
    fetchAPIMock.mockImplementation(async (path: string) => {
      if (path === '/shadow/profile') return { profile: { shadow_id: 'shadow_abc12345' }, saved: true }
      if (path === '/shadow/trades') return TRADES
      return {}
    })
  })

  it('默认选中第一个标的, 只把该标的成交传给 K 线', async () => {
    render(<ShadowAccount />)

    await waitFor(() => expect(fetchAPIMock).toHaveBeenCalledWith('/shadow/trades', expect.anything()))
    const node = await screen.findByTestId('kline')
    // 600519.SH 两笔(1 买 1 卖); 002361.SZ 那笔**不能**混进来
    expect(node.textContent).toBe('sym=600519.SH;markers=2026-01-05/buy,2026-01-07/sell')
    expect(chartProps.at(-1).tradeMarkers).toHaveLength(2)
  })

  it('无成交明细 → 不渲染图表, 给"先上传"的说明', async () => {
    fetchAPIMock.mockImplementation(async (path: string) => {
      if (path === '/shadow/profile') return { profile: null, saved: false }
      if (path === '/shadow/trades') {
        return { saved: false, symbols: [], trades: [], total: 0, capped: false, note: '尚未上传交割单, 或该次分析早于成交明细落库(重新上传一次即可)' }
      }
      return {}
    })

    render(<ShadowAccount />)

    await waitFor(() => expect(screen.getByText(/尚未上传交割单/)).toBeTruthy())
    expect(screen.queryByTestId('kline')).toBeNull()
  })

  it('capped=true 时显式提示"仅保留最近 400 笔"', async () => {
    fetchAPIMock.mockImplementation(async (path: string) => {
      if (path === '/shadow/profile') return { profile: null, saved: false }
      if (path === '/shadow/trades') return { ...TRADES, capped: true }
      return {}
    })

    render(<ShadowAccount />)

    await waitFor(() => expect(screen.getByText(/仅保留最近 400 笔/)).toBeTruthy())
  })
})
