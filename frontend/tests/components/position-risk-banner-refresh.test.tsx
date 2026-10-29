// @vitest-environment jsdom
/**
 * DefenseLineBanner · 行情/市场状态必须自己续期（2026-10-29 独立复审 F2）。
 *
 * 旧实现只在挂载时各取一次 `/portfolio/summary`、`/stocks/markets/status`、`/quotes/{symbol}`。
 * 页面长期开着（持仓页最典型的用法就是整天开着）时会：
 *   ① 09:31 取到的价一直挂着，14:00 仍然拿它判"已击穿"；
 *   ② 市场墙钟也停在 09:31，于是"报价距市场时钟多久"永远算出 0 分钟 ⇒ 旧价被判成实时；
 *   ③ 价格其实早涨回防线之上，告警条却持续报警 —— 用户按旧价执行纪律。
 *
 * 现在的口径：
 *   - 告警条按固定节奏**自己**续期（与页面 autoRefresh 开关无关），页签隐藏时停表、回前台补一次；
 *   - 续期失败（网络/服务端错误）⇒ 不许继续拿旧快照判破位，降级成「时效未核实」；
 *   - 卸载 / 切标的必须清理定时器，不留后台请求。
 *
 * 这里用**真实 timer 推进**（vi.advanceTimersByTime）验证周期行为，不靠 sleep。
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({ fetchAPI: api.fetchAPI }))

import { DEFENSE_QUOTE_REFRESH_MS, DefenseLineBanner } from '@/components/DefenseLineBanner'

const MARKETS = [{ code: 'CN', name: 'A股', status: 'trading', local_time: '09:31' }]

const SUMMARY = {
  accounts: [
    {
      id: 1,
      name: '主账户',
      positions: [{ id: 11, symbol: '600111', market: 'CN', cost_price: 36.52, defense_price: 34 }],
    },
  ],
}

/** 把微任务链推完（组件内是 await 链，不是定时器）。 */
async function flush(rounds = 12) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

type QuoteHandler = () => { current_price: number; quote_time: string; daily_pnl_period: string } | null

function mockApi(opts: { quote: QuoteHandler; markets?: () => unknown }) {
  api.fetchAPI.mockImplementation((url: string) => {
    const u = String(url)
    if (u.includes('/portfolio/summary')) return Promise.resolve(SUMMARY)
    if (u.includes('/stocks/markets/status')) {
      const rows = opts.markets ? opts.markets() : MARKETS
      return rows instanceof Error ? Promise.reject(rows) : Promise.resolve(rows)
    }
    if (u.includes('/quotes/')) {
      const payload = opts.quote()
      return payload ? Promise.resolve(payload) : Promise.reject(new Error('no quote'))
    }
    return Promise.reject(new Error(`unexpected url ${url}`))
  })
}

const quoteCalls = () => api.fetchAPI.mock.calls.filter((c) => String(c[0]).includes('/quotes/')).length
const statusCalls = () =>
  api.fetchAPI.mock.calls.filter((c) => String(c[0]).includes('/stocks/markets/status')).length

beforeEach(() => {
  api.fetchAPI.mockReset()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('DefenseLineBanner · 行情续期', () => {
  it('盘中价格回到防线之上后，告警条自己刷新，不再持续报破位', async () => {
    vi.useFakeTimers()
    let price = 33 // 先击穿防线 34
    mockApi({
      quote: () => ({ current_price: price, quote_time: '2026-10-29 09:31:00', daily_pnl_period: 'today' }),
      markets: () => [{ code: 'CN', status: 'trading', local_time: '09:31' }],
    })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    expect(screen.getByTestId('defense-line-banner').textContent).toContain('已击穿')

    // 14:00 行情回到 40（防线 34 之上）：推进到下一次续期
    price = 40
    const statusBefore = statusCalls()
    const quoteBefore = quoteCalls()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_QUOTE_REFRESH_MS)
    })
    await flush()

    // 必须真的又取了价与市场墙钟（旧实现一次都不再取）
    expect(quoteCalls()).toBeGreaterThan(quoteBefore)
    expect(statusCalls()).toBeGreaterThan(statusBefore)
    const banner = screen.getByTestId('defense-line-banner').textContent ?? ''
    expect(banner).toContain('¥40')
    expect(banner).not.toContain('已击穿')
  })

  it('市场墙钟与报价同步前进：09:31 的旧价到点后不再被判「实时破位」', async () => {
    vi.useFakeTimers()
    let clock = '09:31'
    mockApi({
      // 价格一直是 33（击穿），但报价时刻停在 09:31 —— 市场墙钟推进后它就该过期
      quote: () => ({ current_price: 33, quote_time: '2026-10-29 09:31:00', daily_pnl_period: 'today' }),
      markets: () => [{ code: 'CN', status: 'trading', local_time: clock }],
    })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    expect(screen.getByTestId('defense-line-banner').textContent).toContain('已击穿')

    clock = '14:00'
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_QUOTE_REFRESH_MS)
    })
    await flush()

    const stale = screen.getByTestId('defense-line-stale')
    expect(stale.textContent).toContain('时效未证实')
    expect(stale.textContent).not.toContain('已击穿')
  })

  it('续期失败 → 降级「时效未核实」，不许拿旧快照继续判破位', async () => {
    vi.useFakeTimers()
    let fail = false
    mockApi({
      quote: () => (fail ? null : { current_price: 33, quote_time: '2026-10-29 09:31:00', daily_pnl_period: 'today' }),
      markets: () =>
        fail ? new Error('status down') : [{ code: 'CN', status: 'trading', local_time: '09:31' }],
    })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    expect(screen.getByTestId('defense-line-banner').textContent).toContain('已击穿')

    fail = true
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_QUOTE_REFRESH_MS)
    })
    await flush()

    expect(screen.queryByText(/已击穿你的防守线/)).toBeNull()
    const unverified = screen.getByTestId('defense-line-stale')
    expect(unverified.textContent).toContain('时效未核实')
    expect(unverified.textContent).toContain('刷新失败')
  })

  it('卸载后不再续期（不留后台请求）', async () => {
    vi.useFakeTimers()
    mockApi({
      quote: () => ({ current_price: 33, quote_time: '2026-10-29 09:31:00', daily_pnl_period: 'today' }),
    })
    const view = render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    const before = quoteCalls() + statusCalls()
    view.unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(quoteCalls() + statusCalls()).toBe(before)
  })
})
