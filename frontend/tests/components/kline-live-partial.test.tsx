// @vitest-environment jsdom
//
// feat(2026-10-12) 盘中未定型徽章钉: 后端读时合成的今日日 K 带 `live_bar.partial=true`
// (分钟聚合/实时日线 forming, 18:00 才批量定型), 前端必须在新鲜度那行显式标『盘中未定型』
// —— 诚实标注, 绝不把合成/未定型值当已收盘的真值。UI 铁律: 小字不卡片化(仅一段 muted 小字)。
//
// 全 mock, 不碰真实网络: 图表库由 vite.config.ts 的 test.alias 换成假实现(见 stubs/)。
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

const H = vi.hoisted(() => {
  function make() {
    let resolve!: (v: unknown) => void
    let reject!: (e: unknown) => void
    const promise = new Promise<unknown>((res, rej) => {
      resolve = res
      reject = rej
    })
    return { promise, resolve, reject }
  }
  return {
    main: new Map<string, ReturnType<typeof make>>(),
    make,
  }
})

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string) => {
    if (String(url).includes('/summary')) return Promise.resolve({})
    const sym = /\/klines\/([^/?]+)/.exec(String(url))?.[1] ?? ''
    const d = H.main.get(sym)
    return d ? d.promise : Promise.resolve({ klines: [] })
  }),
}))

import KlineChart from '@sida/biz-ui/components/KlineChart'

function bars(n: number, base = 10) {
  return Array.from({ length: n }, (_, i) => ({
    date: `2026-03-${String(i + 1).padStart(2, '0')}`,
    open: base + i,
    high: base + i + 0.5,
    low: base + i - 0.5,
    close: base + i + 0.2,
    volume: 1000 + i,
  }))
}

function renderChart(symbol: string) {
  return render(
    <KlineChart symbol={symbol} market="CN" initialInterval="1d" initialDays={120} height={420} />,
  )
}

beforeEach(() => {
  H.main.clear()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('KlineChart 盘中未定型徽章(2026-10-12)', () => {
  it('live_bar.partial=true → 显式标『盘中未定型』+ 来源(不卡片化小字)', async () => {
    const main = H.make()
    H.main.set('600000', main)

    renderChart('600000')
    await act(async () => {
      main.resolve({
        klines: bars(5),
        today_bar: 'live',
        live_bar: { partial: true, source: 'live_synthesized', as_of: '2026-10-12T15:00:00+08:00' },
      })
    })

    await waitFor(() => expect(screen.getByTestId('kline-live-partial')).toBeTruthy())
    const badge = screen.getByTestId('kline-live-partial')
    expect(badge.textContent).toContain('盘中未定型')
    expect(badge.textContent).toContain('live_synthesized')
    expect(badge.textContent).toContain('2026-10-12 15:00')
  })

  it('无 live_bar(已定型/未合成) → 不出现徽章', async () => {
    const main = H.make()
    H.main.set('600001', main)

    renderChart('600001')
    await act(async () => {
      main.resolve({ klines: bars(5), today_bar: 'pg' })
    })

    await waitFor(() => expect(screen.getByTestId('kline-freshness')).toBeTruthy())
    expect(screen.queryByTestId('kline-live-partial')).toBeNull()
  })
})
