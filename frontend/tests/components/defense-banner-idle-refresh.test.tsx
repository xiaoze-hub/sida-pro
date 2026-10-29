// @vitest-environment jsdom
/**
 * DefenseLineBanner · 休市降频（2026-10-29）。
 *
 * 告警条按 20s 自己续期（F2），这对盘中是必须的；但收盘后价格不再变化，
 * 每个开着的持仓页仍各自 20s 打一次 `/quotes/{symbol}` 只是纯流量（行情源按分钟级限流）。
 *
 * 口径：
 *   - 服务器判定**相关市场全部处于「不可即将开盘」的休市态**（after_hours / closed）
 *     ⇒ 报价续期降到 `DEFENSE_IDLE_REFRESH_MS`；
 *   - 盘中 / 午休 / 盘前 / 状态未知（无法证明休市）⇒ 保持 20s（宁可多取，不可漏判）；
 *   - 市场墙钟与持仓行不降频到不可用：墙钟跟着同一个 tick，间隔变大但仍有；回前台会补一次。
 */
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({ fetchAPI: api.fetchAPI }))

import {
  DEFENSE_IDLE_REFRESH_MS,
  DEFENSE_QUOTE_REFRESH_MS,
  DefenseLineBanner,
} from '@/components/DefenseLineBanner'

const SUMMARY = {
  accounts: [
    {
      id: 1,
      name: '主账户',
      positions: [{ id: 11, symbol: '600111', market: 'CN', cost_price: 36.52, defense_price: 34 }],
    },
  ],
}

async function flush(rounds = 12) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

function mockApi(status: () => string) {
  api.fetchAPI.mockImplementation((url: string) => {
    const u = String(url)
    if (u.includes('/portfolio/summary')) return Promise.resolve(SUMMARY)
    if (u.includes('/stocks/markets/status')) {
      return Promise.resolve([
        { code: 'CN', name: 'A股', status: status(), local_time: '16:00', local_date: '2026-10-29' },
      ])
    }
    if (u.includes('/quotes/')) {
      return Promise.resolve({
        current_price: 33,
        quote_time: '2026-10-29 15:00:00',
        quote_date: '2026-10-29',
        daily_pnl_period: 'today',
      })
    }
    return Promise.reject(new Error(`unexpected url ${url}`))
  })
}

const quoteCalls = () => api.fetchAPI.mock.calls.filter((c) => String(c[0]).includes('/quotes/')).length

beforeEach(() => {
  api.fetchAPI.mockReset()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('DefenseLineBanner · 休市降频', () => {
  it('收盘后（after_hours）报价续期降到 IDLE 间隔，20s 不再打行情', async () => {
    vi.useFakeTimers()
    mockApi(() => 'after_hours')
    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    expect(DEFENSE_IDLE_REFRESH_MS).toBeGreaterThan(DEFENSE_QUOTE_REFRESH_MS)

    const before = quoteCalls()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_QUOTE_REFRESH_MS)
    })
    await flush()
    expect(quoteCalls()).toBe(before)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_IDLE_REFRESH_MS)
    })
    await flush()
    expect(quoteCalls()).toBeGreaterThan(before)
  })

  it('周末休市（closed）同样降频', async () => {
    vi.useFakeTimers()
    mockApi(() => 'closed')
    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()

    const before = quoteCalls()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_QUOTE_REFRESH_MS)
    })
    await flush()
    expect(quoteCalls()).toBe(before)
  })

  it('交易中不降频：20s 必须续期（漏判比多取一轮更贵）', async () => {
    vi.useFakeTimers()
    mockApi(() => 'trading')
    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()

    const before = quoteCalls()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_QUOTE_REFRESH_MS)
    })
    await flush()
    expect(quoteCalls()).toBeGreaterThan(before)
  })

  it('盘前（pre_market）不降频：马上要开盘，不能等两分钟', async () => {
    vi.useFakeTimers()
    mockApi(() => 'pre_market')
    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()

    const before = quoteCalls()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_QUOTE_REFRESH_MS)
    })
    await flush()
    expect(quoteCalls()).toBeGreaterThan(before)
  })
})
