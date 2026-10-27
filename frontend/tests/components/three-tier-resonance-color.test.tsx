// @vitest-environment jsdom
// 铁三角共振三态配色(2026-10-24 P0 设计走查):
//   warning 曾被渲染成 `text-green-500` —— 一个「警告」被读成「安全/上涨」, 语义反置。
//   opportunity 的 `text-red-500` **保留**: 按 A 股买红卖绿, 机会=红是正确的, 不是缺陷。
// 断言到 className 级别, 不看文案, 防止以后换词不换色又把语义翻回去。
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
}))

import { ThreeTierVerdictRow } from '@/components/ThreeTierVerdictRow'

/** 按路由返回, 只让 decision-pioneer 携带共振三态, 其余给最小可用值。 */
function route(warning: string, state = '共振态') {
  mocks.fetchAPI.mockImplementation((path: string) => {
    if (path.startsWith('/market/phase')) {
      return Promise.resolve({ current_phase: { phase: 'ferment', label: '发酵' } })
    }
    if (path.startsWith('/catalyst/calendar')) return Promise.resolve({ calendar: [] })
    if (path.includes('/wyckoff')) return Promise.resolve({ verdict: { stage: 'unknown' } })
    if (path.includes('/summary')) return Promise.resolve({ cancel_order_intent: { available: false } })
    if (path.startsWith('/decision-pioneer/')) {
      return Promise.resolve({
        resonance_state: state,
        resonance_action: '仅作风控提示, 不构成买卖指令',
        resonance_warning: warning,
      })
    }
    return Promise.resolve({})
  })
}

/** 共振态那个 span: 带 title=action 的可读源头, 且文本就是 state。 */
function resonanceSpan(): HTMLElement {
  const el = screen.getByTitle('仅作风控提示, 不构成买卖指令')
  expect(el.textContent).toBe('共振态')
  return el
}

describe('ThreeTierVerdictRow 共振三态配色', () => {
  afterEach(() => {
    cleanup()
    mocks.fetchAPI.mockReset()
  })

  it('warning 态 = 告警色, 不得为绿色(语义反置修复)', async () => {
    route('warning')
    render(<ThreeTierVerdictRow symbol="600519" market="cn" />)

    await screen.findByText('共振态')
    const cls = resonanceSpan().className
    expect(cls).not.toMatch(/text-green-/)
    expect(cls).not.toMatch(/text-emerald-/) // 绿色系一律不许出现在 warning 态
    expect(cls).toMatch(/text-amber-500/)
  })

  it('warning 与 caution 不同色(否则两态不可区分)', async () => {
    route('warning')
    render(<ThreeTierVerdictRow symbol="600519" market="cn" />)
    await screen.findByText('共振态')
    const warnCls = resonanceSpan().className
    cleanup()

    route('caution')
    render(<ThreeTierVerdictRow symbol="600519" market="cn" />)
    await screen.findByText('共振态')
    const cautionCls = resonanceSpan().className

    expect(warnCls).not.toBe(cautionCls)
    expect(cautionCls).toMatch(/text-yellow-500/)
  })

  it('opportunity 态仍为 text-red-500(A 股买红: 机会=红是正确语义, 不得改动)', async () => {
    route('opportunity')
    render(<ThreeTierVerdictRow symbol="600519" market="cn" />)

    await screen.findByText('共振态')
    expect(resonanceSpan().className).toMatch(/text-red-500/)
  })

  it('unknown 态灰显, 不给方向色', async () => {
    route('unknown')
    render(<ThreeTierVerdictRow symbol="600519" market="cn" />)

    await screen.findByText('共振态')
    const cls = resonanceSpan().className
    expect(cls).toMatch(/text-muted-foreground/)
    expect(cls).not.toMatch(/text-(green|emerald|red|amber|yellow)-/)
  })
})
