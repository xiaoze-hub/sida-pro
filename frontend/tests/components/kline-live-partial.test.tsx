// @vitest-environment jsdom
//
// feat(2026-10-12) 盘中未定型徽章钉: 后端读时合成的今日日 K 带 `live_bar.partial=true`
// (分钟聚合/实时日线 forming, 18:00 才批量定型), 前端必须在新鲜度那行显式标『盘中未定型』
// —— 诚实标注, 绝不把合成/未定型值当已收盘的真值。UI 铁律: 小字不卡片化(仅一段 muted 小字)。
//
// 全 mock, 不碰真实网络: 图表库由 vite.config.ts 的 test.alias 换成假实现(见 stubs/)。
import { act, fireEvent, cleanup, render, screen, waitFor } from '@testing-library/react'
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

function renderChart(symbol: string, { instrument, interval = '1d' }: { instrument?: 'index'; interval?: '1d' | '1w' | '1mth' } = {}) {
  return render(
    <KlineChart symbol={symbol} market="CN" instrument={instrument} initialInterval={interval} initialDays={120} height={420} />,
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
    expect(screen.getAllByText('盘中未定型')).toHaveLength(1)
    expect(badge.textContent).toContain('分钟线聚合')
    expect(badge.textContent).not.toContain('live_synthesized')
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


describe('KlineChart 主图数据状态', () => {
  it('个股缺当日 K（休市和盘前也会返回 missing）只标实际数据日，不报异常', async () => {
    const main = H.make()
    H.main.set('600002', main)
    renderChart('600002')
    expect(screen.getByTestId('kline-canvas-state').textContent).toContain('正在读取')
    await act(async () => {
      main.resolve({ klines: bars(5), source: 'pg_klines_hypertable', today_bar: 'missing' })
    })
    const status = screen.getByTestId('kline-freshness')
    expect(status.getAttribute('data-phase')).toBe('historical')
    expect(status.textContent).toContain('数据截至 2026-03-05')
    expect(status.textContent).toContain('行情历史库')
    expect(screen.getByText('历史数据').classList.contains('text-role-watch')).toBe(false)
    expect(status.textContent).not.toMatch(/未取得|延迟|暂不可用/)
    expect(screen.queryByTestId('kline-canvas-state')).toBeNull()
  })

  it.each([{ instrument: 'index' as const, payload: {} }, { instrument: undefined, payload: { is_index: true } }])(
    '指数未返回当日标记时，不把历史末根显示为普通已载入（$instrument）',
    async ({ instrument, payload }) => {
      const main = H.make()
      H.main.set('000001', main)
      renderChart('000001', { instrument })
      await act(async () => {
        main.resolve({ klines: bars(5).reverse(), source: 'private_source_key', ...payload })
      })
      const status = screen.getByTestId('kline-freshness')
      expect(status.getAttribute('data-phase')).toBe('historical')
      expect(status.textContent).toContain('数据截至 2026-03-05')
      expect(status.textContent).toContain('来源：未标注')
      expect(status.textContent).not.toContain('private_source_key')
    },
  )

  it('已收盘的指数末根有当日标记，不显示未定型或历史', async () => {
    const main = H.make()
    H.main.set('000001', main)
    renderChart('000001', { instrument: 'index' })
    await act(async () => {
      main.resolve({
        klines: bars(5), today_bar: 'live',
        live_bar: { partial: false, source: 'live_index', as_of: '2026-03-05T16:00:00+08:00' },
      })
    })
    const status = screen.getByTestId('kline-freshness')
    expect(status.getAttribute('data-phase')).toBe('available')
    expect(status.textContent).toContain('末根来源：指数行情源')
    expect(screen.queryByTestId('kline-live-partial')).toBeNull()
  })

  it.each(['1w', '1mth'] as const)('%s 聚合周期不按今日 K 线推断新鲜度', async (interval) => {
    const main = H.make()
    H.main.set('000001', main)
    renderChart('000001', { interval, instrument: 'index' })
    await act(async () => { main.resolve({ klines: bars(5), is_index: true, today_bar: 'missing' }) })
    expect(screen.getByTestId('kline-freshness').getAttribute('data-phase')).toBe('available')
  })

  it('当日未定型 K 无法绘制时，不把它的标签挂到上一根', async () => {
    const main = H.make()
    H.main.set('600005', main)
    renderChart('600005')
    const candles = bars(5)
    candles[4].close = Number.NaN
    await act(async () => {
      main.resolve({
        klines: candles, today_bar: 'live',
        live_bar: { partial: true, source: 'live_synthesized', as_of: '2026-03-05T14:30:00+08:00' },
      })
    })
    const status = screen.getByTestId('kline-freshness')
    expect(status.getAttribute('data-phase')).toBe('historical')
    expect(status.textContent).toContain('数据截至 2026-03-04')
    expect(screen.queryByTestId('kline-live-partial')).toBeNull()
  })

  it('取数失败有可操作空态；重试绕缓存并可恢复成已载入', async () => {
    const first = H.make()
    H.main.set('600003', first)
    renderChart('600003')
    await act(async () => { first.reject(new Error('private upstream details')) })
    const empty = screen.getByTestId('kline-canvas-state')
    expect(empty.textContent).toContain('暂时无法读取')
    expect(document.body.textContent).not.toContain('private upstream details')
    const retry = H.make()
    H.main.set('600003', retry)
    fireEvent.click(screen.getByRole('button', { name: '重试 K 线' }))
    await act(async () => { retry.resolve({ klines: bars(5), today_bar: 'pg' }) })
    expect(screen.getByTestId('kline-freshness').getAttribute('data-phase')).toBe('available')
    expect(screen.queryByTestId('kline-canvas-state')).toBeNull()
  })

  it('成功返回空数组不当成请求失败，也不显示旧日期', async () => {
    const main = H.make()
    H.main.set('600004', main)
    renderChart('600004')
    await act(async () => { main.resolve({ klines: [], today_bar: 'missing' }) })
    expect(screen.getByTestId('kline-freshness').getAttribute('data-phase')).toBe('empty')
    expect(screen.getByTestId('kline-canvas-state').textContent).toContain('当前周期暂无')
    expect(screen.queryByText(/数据截至/)).toBeNull()
  })
})
