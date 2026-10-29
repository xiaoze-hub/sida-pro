// @vitest-environment jsdom
/**
 * DefenseLineBanner · 多账户 / 陈旧行情回归（2026-10-29 评审补充）。
 *
 * 评审暴露的两个错判：
 *  ① 旧实现 `rows.find(p => p.symbol === symbol)` 只取**第一个**账户的 defense_price。
 *     同一只票在主账户设 34、备用账户设 25、现价 33 时，告警条按账户顺序在
 *     「已击穿」和「防守线（安全）」之间来回翻，且与持仓页逐只清单互相打架。
 *     ⇒ 必须按账户逐条显示各自防线，不猜、不合并。
 *  ② `/quotes/{symbol}` 返回的可能是上一交易日甚至更早的报价。旧实现拿它当实时价
 *     直接判「已击穿」。⇒ 报价不是当日（`daily_pnl_period !== 'today'`）时只能标
 *     「行情滞后」并给出真实行情日期，不许出破位结论。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({ fetchAPI: api.fetchAPI }))

import { DefenseLineBanner } from '@/components/DefenseLineBanner'

type Row = {
  id: number
  symbol: string
  cost_price: number
  defense_price: number | null
}
type Acc = { id: number; name: string; positions: Row[] }

function summary(accounts: Acc[]) {
  return { accounts }
}

function quote(over: Record<string, unknown> = {}) {
  return {
    symbol: '600111',
    current_price: 33,
    quote_date: '2026-10-29',
    quote_time: '2026-10-29 15:00:00',
    daily_pnl_period: 'today',
    ...over,
  }
}

function mockApi(accounts: Acc[], quotePayload: Record<string, unknown>) {
  api.fetchAPI.mockImplementation((url: string) => {
    if (String(url).includes('/portfolio/summary')) return Promise.resolve(summary(accounts))
    if (String(url).includes('/quotes/')) return Promise.resolve(quotePayload)
    return Promise.reject(new Error(`unexpected url ${url}`))
  })
}

const renderBanner = () =>
  render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)

beforeEach(() => {
  api.fetchAPI.mockReset()
})

afterEach(() => cleanup())

describe('DefenseLineBanner · 多账户防线', () => {
  it('两账户防线不同（34 / 25，现价 33）→ 逐账户各一条，不取首个', async () => {
    mockApi(
      [
        { id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] },
        { id: 2, name: '备用账户', positions: [{ id: 12, symbol: '600111', cost_price: 36.52, defense_price: 25 }] },
      ],
      quote(),
    )
    renderBanner()

    const multi = await screen.findByTestId('defense-line-multi')
    const text = multi.textContent ?? ''
    expect(text).toContain('主账户')
    expect(text).toContain('备用账户')
    // 主账户防线 34 > 现价 33 → 击穿；备用账户防线 25 < 现价 33 → 安全
    expect(multi.querySelector('[data-testid="defense-line-account-1"]')?.textContent).toContain('已击穿')
    expect(multi.querySelector('[data-testid="defense-line-account-2"]')?.textContent).not.toContain('已击穿')
    // 不许把两个账户合成一个结论（旧实现只显示第一条）
    expect(screen.getAllByText(/已击穿你的防守线/).length).toBe(1)
  })

  it('两账户防线相同 → 仍按账户归属但结论一致，并说明覆盖几个账户', async () => {
    mockApi(
      [
        { id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] },
        { id: 2, name: '备用账户', positions: [{ id: 12, symbol: '600111', cost_price: 36.52, defense_price: 34 }] },
      ],
      quote(),
    )
    renderBanner()
    const multi = await screen.findByTestId('defense-line-multi')
    expect(multi.textContent).toContain('2 个账户')
    expect(multi.textContent).toContain('已击穿')
    // 防线值只出现 34 这一档，且不出现 25
    expect(multi.textContent).not.toContain('25')
  })

  it('单账户行为不变：一条告警条，带防线/现价/浮亏', async () => {
    mockApi(
      [{ id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] }],
      quote(),
    )
    renderBanner()
    const banner = await screen.findByTestId('defense-line-banner')
    expect(banner.textContent).toContain('已击穿你的防守线')
    expect(banner.textContent).toContain('34.00')
    expect(banner.textContent).toContain('33.00')
  })
})

describe('DefenseLineBanner · 陈旧行情', () => {
  it('上一交易日报价跌破防线 → 只标「行情滞后」，不出破位结论', async () => {
    mockApi(
      [{ id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] }],
      quote({
        current_price: 33,
        daily_pnl_period: 'previous_trading_day',
        quote_date: '2026-10-28',
        quote_time: '2026-10-28 15:00:00',
      }),
    )
    renderBanner()
    const stale = await screen.findByTestId('defense-line-stale')
    expect(stale.textContent).toContain('行情滞后')
    expect(stale.textContent).toContain('2026-10-28')
    expect(stale.textContent).not.toContain('已击穿')
    expect(screen.queryByText(/已击穿你的防守线/)).toBeNull()
  })

  it('行情时点未知（unknown）同样不判破位', async () => {
    mockApi(
      [{ id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] }],
      quote({ current_price: 33, daily_pnl_period: 'unknown', quote_date: null, quote_time: null }),
    )
    renderBanner()
    const stale = await screen.findByTestId('defense-line-stale')
    expect(stale.textContent).toContain('行情滞后')
    expect(screen.queryByText(/已击穿你的防守线/)).toBeNull()
  })
})

describe('DefenseLineBanner · 未设置与读取失败', () => {
  it('所有账户都没设防线 → 未设置提示（不告警），不给单一默认值', async () => {
    mockApi(
      [
        { id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: null }] },
        { id: 2, name: '备用账户', positions: [{ id: 12, symbol: '600111', cost_price: 36.52, defense_price: null }] },
      ],
      quote(),
    )
    renderBanner()
    const unset = await screen.findByTestId('defense-line-unset')
    expect(unset.textContent).toContain('未设置防守线')
    expect(unset.textContent).not.toContain('已击穿')
  })

  it('部分账户设了防线：已设的照常判定，未设的要点名，不许当成「全部未设」', async () => {
    mockApi(
      [
        { id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] },
        { id: 2, name: '备用账户', positions: [{ id: 12, symbol: '600111', cost_price: 36.52, defense_price: null }] },
      ],
      quote(),
    )
    renderBanner()
    const multi = await screen.findByTestId('defense-line-multi')
    expect(multi.textContent).toContain('主账户')
    expect(multi.textContent).toContain('已击穿')
    expect(multi.textContent).toContain('未设置防守线')
  })

  it('持仓读取失败 → 说未知，不冒充未设置', async () => {
    api.fetchAPI.mockImplementation((url: string) => {
      if (String(url).includes('/portfolio/summary')) return Promise.reject(new Error('boom'))
      if (String(url).includes('/quotes/')) return Promise.resolve(quote())
      return Promise.reject(new Error('unexpected'))
    })
    renderBanner()
    const unknown = await screen.findByTestId('defense-line-unknown')
    expect(unknown.textContent).toContain('状态未知')
  })

  it('取不到现价 → 显式降级，不静默消失', async () => {
    mockApi(
      [{ id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] }],
      quote({ current_price: null }),
    )
    renderBanner()
    const degraded = await screen.findByTestId('defense-line-degraded')
    await waitFor(() => expect(degraded.textContent).toContain('防守线无法计算'))
  })
})
