// @vitest-environment jsdom
/**
 * DefenseLineBanner · 多账户 / 多市场 / 陈旧行情回归（2026-10-29 评审补充 + 三次复审）。
 *
 * 评审暴露的错判：
 *  ① 旧实现 `rows.find(p => p.symbol === symbol)` 只取**第一个**账户的 defense_price。
 *     同一只票在主账户设 34、备用账户设 25、现价 33 时，告警条按账户顺序在
 *     「已击穿」和「防守线（安全）」之间来回翻，且与持仓页逐只清单互相打架。
 *     ⇒ 必须按账户逐条显示各自防线，不猜、不合并。
 *  ② `/quotes/{symbol}` 返回的可能是上一交易日甚至更早的报价。⇒ 报价不是当日时只能标
 *     「时效未证实」并给出真实行情日期，不许出破位结论。
 *
 * 三次复审（本轮新增，先红后修）：
 *  ③ **取价要带市场**：旧代码打 `/quotes/600111`（后端默认 CN），港美股持仓永远拿不到价。
 *     必须按持仓行自己的 `market` 取价，且同一代码在不同市场互不串价。
 *  ④ **成本/预设按账户**：旧实现所有账户共用父组件传来的第一份成本，
 *     预设价算成另一个账户的价位。现在每行用自己的 `cost_price`。
 *  ⑤ **日内陈旧**：09:31 的报价在 14:00 不能显示「已击穿」——判时刻要服务器市场墙钟。
 *     拿不到市场时钟时同样不判（保守未知），不退回「today 就是实时」。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({ fetchAPI: api.fetchAPI }))

import { DefenseLineBanner } from '@/components/DefenseLineBanner'

type Row = {
  id: number
  symbol: string
  market?: string
  cost_price: number | null
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

const MARKETS = [
  { code: 'CN', name: 'A股', status: 'trading', local_time: '15:03', local_date: '2026-10-29' },
  { code: 'HK', name: '港股', status: 'trading', local_time: '15:03', local_date: '2026-10-29' },
  { code: 'US', name: '美股', status: 'trading', local_time: '15:03', local_date: '2026-10-29' },
]

type QuoteHandler = (url: string) => Record<string, unknown> | null

function mockApi(
  accounts: Acc[],
  quotePayload: Record<string, unknown> | QuoteHandler,
  markets: unknown = MARKETS,
) {
  api.fetchAPI.mockImplementation((url: string) => {
    const u = String(url)
    if (u.includes('/portfolio/summary')) return Promise.resolve(summary(accounts))
    if (u.includes('/stocks/markets/status')) {
      return markets instanceof Error
        ? Promise.reject(markets)
        : Promise.resolve(markets)
    }
    if (u.includes('/quotes/')) {
      const payload =
        typeof quotePayload === 'function' ? quotePayload(u) : quotePayload
      return payload ? Promise.resolve(payload) : Promise.reject(new Error('no quote'))
    }
    return Promise.reject(new Error(`unexpected url ${url}`))
  })
}

const renderBanner = () =>
  render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)

const quoteUrls = () =>
  api.fetchAPI.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/quotes/'))

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
    expect(multi.textContent).not.toContain('25')
  })

  it('单账户行为不变：一条告警条，带防线/现价/浮动', async () => {
    mockApi(
      [{ id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] }],
      quote(),
    )
    renderBanner()
    const banner = await screen.findByTestId('defense-line-banner')
    expect(banner.textContent).toContain('已击穿你的防守线')
    expect(banner.textContent).toContain('防线 ¥34')
    expect(banner.textContent).toContain('现价 ¥33')
    expect(banner.textContent).not.toContain('**')
  })
})

describe('DefenseLineBanner · 成本按账户（不许共用父组件的第一份成本）', () => {
  it('两账户成本不同：每行成本与 -8% 预设各自算', async () => {
    mockApi(
      [
        { id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 100, defense_price: null }] },
        { id: 2, name: '备用账户', positions: [{ id: 12, symbol: '600111', cost_price: 50, defense_price: null }] },
      ],
      quote(),
    )
    renderBanner()
    const unset = await screen.findByTestId('defense-line-unset')
    const main = unset.querySelector('[data-testid="defense-line-account-1"]')?.textContent ?? ''
    const backup = unset.querySelector('[data-testid="defense-line-account-2"]')?.textContent ?? ''
    expect(main).toContain('100.00')
    expect(main).toContain('92') // 100 * 0.92
    expect(backup).toContain('50.00')
    expect(backup).toContain('46') // 50 * 0.92
    // 父组件传的是 36.52，任何一行都不许用它冒充账户成本
    expect(unset.textContent).not.toContain('36.52')
  })

  it('打开某账户的设置弹窗：显示的是**该账户**的成本与币种', async () => {
    mockApi(
      [
        { id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 100, defense_price: null }] },
        { id: 2, name: '港股账户', positions: [{ id: 12, symbol: '600111', market: 'HK', cost_price: 50, defense_price: null }] },
      ],
      quote(),
    )
    renderBanner()
    const unset = await screen.findByTestId('defense-line-unset')
    const setHk = unset.querySelector('[data-testid="defense-line-set-2"]') as HTMLElement
    setHk.click()
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(dialog.textContent).toContain('50.00'))
    expect(dialog.textContent).toContain('HK$')
    expect(dialog.textContent).not.toContain('36.52')
  })
})

describe('DefenseLineBanner · 按市场取价', () => {
  it('港股持仓请求带 market=HK（旧实现不传 → 后端按 CN 解析 → 永远缺价）', async () => {
    mockApi(
      [
        {
          id: 1,
          name: '港股账户',
          positions: [{ id: 11, symbol: '00700', market: 'HK', cost_price: 360, defense_price: 350 }],
        },
      ],
      quote({ symbol: '00700', current_price: 340 }),
    )
    render(<DefenseLineBanner symbol="00700" cost={null} hasPosition />)
    const banner = await screen.findByTestId('defense-line-banner')
    expect(quoteUrls()).toContain('/quotes/00700?market=HK')
    expect(quoteUrls()).not.toContain('/quotes/00700')
    expect(banner.textContent).toContain('HK$340')
    expect(banner.textContent).toContain('HK$350')
    expect(banner.textContent).toContain('已击穿')
  })

  it('同标的多市场：每个市场各取一次价，互不串价', async () => {
    mockApi(
      [
        { id: 1, name: 'A股账户', positions: [{ id: 11, symbol: '000001', market: 'CN', cost_price: 12, defense_price: 11 }] },
        { id: 2, name: '港股账户', positions: [{ id: 12, symbol: '000001', market: 'HK', cost_price: 30, defense_price: 20 }] },
      ],
      (url) => {
        if (url.includes('market=HK')) return quote({ symbol: '000001', current_price: 25 })
        return quote({ symbol: '000001', current_price: 10 })
      },
    )
    render(<DefenseLineBanner symbol="000001" cost={12} hasPosition />)
    const multi = await screen.findByTestId('defense-line-multi')
    const cn = multi.querySelector('[data-testid="defense-line-account-1"]')?.textContent ?? ''
    const hk = multi.querySelector('[data-testid="defense-line-account-2"]')?.textContent ?? ''
    expect(cn).toContain('¥10')
    expect(cn).toContain('已击穿')
    expect(hk).toContain('HK$25')
    expect(hk).not.toContain('已击穿')
  })

  it('某市场无价时明确说「该市场无可用现价」，不冒充别的市场的价', async () => {
    mockApi(
      [
        { id: 1, name: 'A股账户', positions: [{ id: 11, symbol: '000001', market: 'CN', cost_price: 12, defense_price: 11 }] },
        { id: 2, name: '港股账户', positions: [{ id: 12, symbol: '000001', market: 'HK', cost_price: 30, defense_price: 20 }] },
      ],
      (url) => (url.includes('market=HK') ? null : quote({ current_price: 10 })),
    )
    render(<DefenseLineBanner symbol="000001" cost={12} hasPosition />)
    const multi = await screen.findByTestId('defense-line-multi')
    const hk = multi.querySelector('[data-testid="defense-line-account-2"]')?.textContent ?? ''
    expect(hk).toContain('缺现价')
    expect(hk).toContain('HK')
    expect(hk).not.toContain('已击穿')
  })
})

describe('DefenseLineBanner · 陈旧行情', () => {
  it('上一交易日报价跌破防线 → 只标「时效未证实」，不出破位结论', async () => {
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
    expect(stale.textContent).toContain('时效未证实')
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
    expect(stale.textContent).toContain('时效未证实')
    expect(screen.queryByText(/已击穿你的防守线/)).toBeNull()
  })

  it('【三次复审】同日 09:31 报价在 14:03 → 不判破位，并写清滞后与报价时刻', async () => {
    mockApi(
      [{ id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] }],
      quote({ current_price: 33, quote_time: '2026-10-29 09:31:00', quote_date: '2026-10-29' }),
      [{ code: 'CN', status: 'trading', local_time: '14:03', local_date: '2026-10-29' }],
    )
    renderBanner()
    const stale = await screen.findByTestId('defense-line-stale')
    expect(stale.textContent).toContain('时效未证实')
    expect(stale.textContent).toContain('09:31')
    expect(stale.textContent).toContain('滞后')
    expect(screen.queryByText(/已击穿你的防守线/)).toBeNull()
  })

  it('【三次复审】拿不到服务器市场时钟 → 保守说「时点未证实」，不冒充实时', async () => {
    mockApi(
      [{ id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] }],
      quote({ current_price: 33 }),
      new Error('status down'),
    )
    renderBanner()
    const stale = await screen.findByTestId('defense-line-stale')
    expect(stale.textContent).toContain('时点未证实')
    expect(screen.queryByText(/已击穿你的防守线/)).toBeNull()
  })

  it('【三次复审】服务器说已收盘 → 判定可出但不叫实时，标明非交易时段', async () => {
    mockApi(
      [{ id: 1, name: '主账户', positions: [{ id: 11, symbol: '600111', cost_price: 36.52, defense_price: 34 }] }],
      quote({ current_price: 33 }),
      [{ code: 'CN', status: 'after_hours', local_time: '19:40', local_date: '2026-10-29' }],
    )
    renderBanner()
    const banner = await screen.findByTestId('defense-line-banner')
    expect(banner.textContent).toContain('已击穿')
    expect(banner.textContent).toContain('非交易时段')
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
      if (String(url).includes('/stocks/markets/status')) return Promise.resolve(MARKETS)
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
