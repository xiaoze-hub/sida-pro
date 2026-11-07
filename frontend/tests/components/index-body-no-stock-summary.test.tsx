// @vitest-environment jsdom
/**
 * 指数正文(IndexBody)与个股接口的**隔离**(2026-11-07 复审证实缺陷)。
 *
 * 缺陷: `IndexKline` 曾调 `useKlineLayer(symbol, market)`, 打的是
 * `GET /klines/{symbol}/summary?market=CN` —— 该端点**没有 instrument 参数**, 按个股解析,
 * 于是 `000001` 在那边是**平安银行**。指数主图用的是指数源, 图层却来自个股 → 串标的。
 *
 * 本用例渲染**真 IndexBody + 真 KlineChart**(只换 canvas 层), 在 `@sida/api::fetchAPI` 这层
 * 记录每一条真实请求, 钉三件事:
 *  ① 不发任何 `/klines/{s}/summary` 个股摘要请求(指数侧没有该口径);
 *  ② 主图 K 线请求带 `instrument=index`(指数源, 不是个股源);
 *  ③ 分时入口仍在(分时走 `/quotes/minute?...&instrument=index`, 是真实的分钟源, 不受影响)。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// jsdom 无 ResizeObserver(KlineChart 容器尺寸自适应用) → 桩掉
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

const H = vi.hoisted(() => ({ calls: [] as string[] }))

const INDEX_DETAIL = {
  symbol: '000001',
  name: '上证指数',
  market: 'CN',
  quote: {
    current_price: 3218.5,
    change_pct: 0.42,
    change_amount: 13.4,
    prev_close: 3205.1,
    open: 3206.0,
    high: 3222.0,
    low: 3201.0,
    volume: 2.4e10,
    amount: 3.1e11,
  },
  klines: [
    { date: '2026-10-30', open: 3200, close: 3205, high: 3210, low: 3190, volume: 1000 },
    { date: '2026-11-03', open: 3215, close: 3225, high: 3230, low: 3210, volume: 1200 },
  ],
  amount_trend: [
    { date: '2026-10-30', amount: 3.0e11 },
    { date: '2026-11-03', amount: 3.1e11 },
  ],
}

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchScenario: vi.fn(async () => ({ available: false })),
  fetchAPI: vi.fn(async (url: string) => {
    const u = String(url)
    H.calls.push(u)
    if (u.includes('/market/indices/')) return INDEX_DETAIL
    if (u.includes('/market-data/market-capital-flow')) return { total_main_flow: 1.2e10 }
    if (/^\/klines\/[^/?]+\?/.test(u)) return { klines: INDEX_DETAIL.klines }
    return {}
  }),
}))

import IndexBody from '@/pages/workbench/IndexBody'

beforeEach(() => {
  H.calls.length = 0
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('IndexBody · 不发个股 summary, 主图走指数口径', () => {
  it('指数正文接受分享链接指定的周K周期', async () => {
    render(<IndexBody symbol="000001" initialInterval="1w" />)
    await waitFor(() => expect(H.calls.some((u) => u.includes('interval=1w') && u.includes('instrument=index'))).toBe(true))
  })
  it('请求清单里没有 /klines/000001/summary, 主图带 instrument=index', async () => {
    render(<IndexBody symbol="000001" />)

    await waitFor(() => expect(screen.getByText('K线走势')).toBeTruthy())
    await waitFor(() => expect(H.calls.some((u) => u.includes('/klines/000001?'))).toBe(true))
    // 慢车道(350ms)窗口过去后仍然不许有个股摘要
    await new Promise((r) => setTimeout(r, 600))

    expect(H.calls.filter((u) => u.includes('/summary'))).toEqual([])
    const klineCall = H.calls.find((u) => u.includes('/klines/000001?'))
    expect(klineCall).toBe('/klines/000001?market=CN&days=120&interval=1d&instrument=index')
    // 分时入口保留(MinutePane 走 /quotes/minute, 与 K 线分钟档是两条源)
    expect(screen.getByTestId('minute-toggle')).toBeTruthy()
  })
})
