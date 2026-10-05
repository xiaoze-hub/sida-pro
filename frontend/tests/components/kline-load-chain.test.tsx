// @vitest-environment jsdom
//
// perf(2026-10-05) 回归钉: KlineChart 的**加载链**(B1 生产走查 `/stocks/002361` 冷态 settle ~22.7s)。
//
// 真渲染 KlineChart 会建 lightweight-charts 实例(jsdom 无 canvas), 故这里把**图表库**整体替成
// 无副作用的假实现 —— 组件取数/状态机/竞态守卫走**真实代码**, 只把 canvas 层换掉。
//
// 钉四件事(全 mock, 不碰真实网络):
//  ① 错峰/不阻塞: 首帧只发关键画布数据 `/klines/{s}`(主图先出), 慢接口 `/klines/{s}/summary`
//     延后到首帧之后才发(start unmounted → summary 未必第一); 主数据先于摘要请求发出;
//     快摘要在途也不阻塞主图出图; 摘要带**显式 45s 超时**(默认 20s 会在冷启动窗口提前掐断);
//  ② 首帧即挂载: 数据未回时图表壳(周期切换器)+「加载中…」已在(不空白等待);
//  ③ 失败显式空态: 主数据失败 → 显式「错误: …」(不假报有数据, 也不伪装成「无数据」);
//  ④ symbol 切换竞态守卫: 旧标的迟到响应不覆盖新标的(cleanup 置 cancelled)。
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// canvas 层由 `vite.config.ts` 的 `test.alias` 换成 `tests/stubs/lightweight-charts.ts`(假图表库),
// 组件取数/状态机/竞态守卫走**真实代码**。

// jsdom 无 ResizeObserver(KlineChart 容器尺寸自适应用) → 桩掉
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

type Deferred = { promise: Promise<unknown>; resolve: (v: unknown) => void; reject: (e: unknown) => void }

const H = vi.hoisted(() => {
  function make(): Deferred {
    let resolve!: (v: unknown) => void
    let reject!: (e: unknown) => void
    const promise = new Promise<unknown>((res, rej) => { resolve = res; reject = rej })
    return { promise, resolve, reject }
  }
  // 记录每次 fetchAPI 的 url/options(顺序即请求次序)
  return {
    calls: [] as Array<{ url: string; options?: { timeoutMs?: number } }>,
    main: new Map<string, Deferred>(),
    summary: { current: null as Deferred | null },
    make,
  }
})

vi.mock('@panwatch/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string, options?: unknown) => {
    H.calls.push({ url: String(url), options: options as { timeoutMs?: number } })
    if (String(url).includes('/summary')) {
      // 慢接口: 用例决定它何时/是否回来
      return H.summary.current ? H.summary.current.promise : Promise.resolve({})
    }
    const sym = /\/klines\/([^/?]+)/.exec(String(url))?.[1] ?? ''
    const d = H.main.get(sym)
    return d ? d.promise : Promise.resolve({ klines: [] })
  }),
}))

import KlineChart from '@panwatch/biz-ui/components/KlineChart'

/** 生成 n 根可用的日 K(字段含有限 OHLC, 过组件内的 null/NaN 过滤)。 */
function bars(n: number, base = 10) {
  return Array.from({ length: n }, (_, i) => ({
    date: `2026-01-${String(i + 1).padStart(2, '0')}`,
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
  H.calls.length = 0
  H.main.clear()
  H.summary.current = null
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('KlineChart 加载链(perf 回归)', () => {
  it('主数据先发/先出图; 慢摘要错峰后发且带显式 45s 超时(不被默认 20s 掐断)', async () => {
    const main = H.make()
    H.main.set('002636', main)
    H.summary.current = H.make() // 慢摘要永挂起

    renderChart('002636')

    // 首帧: 只发出关键画布数据请求 —— 慢摘要尚未发出(错峰到首帧之后)
    expect(H.calls.map((c) => c.url)).toEqual([
      '/klines/002636?market=CN&days=120&interval=1d',
    ])

    // 主数据回来 ⇒ 主图出图(慢摘要仍挂起 ⇒ 主图不被它阻塞)
    await act(async () => {
      main.resolve({ klines: bars(5) })
    })
    await waitFor(() => expect(screen.getByText('5 根K线')).toBeTruthy())

    // 慢摘要 ~350ms 后才错峰发出; 次序在主数据之后; 显式 45s 超时
    await waitFor(() => expect(H.calls.some((c) => c.url.includes('/summary'))).toBe(true))
    const mainIdx = H.calls.findIndex((c) => c.url.includes('/klines/002636?'))
    const sumIdx = H.calls.findIndex((c) => c.url.includes('/summary'))
    expect(mainIdx).toBeGreaterThanOrEqual(0)
    expect(sumIdx).toBeGreaterThan(mainIdx)
    expect(H.calls[sumIdx].options?.timeoutMs).toBe(45000)
  })

  it('首帧即挂载: 数据未回时图表壳与控件已在(不空白等待)', () => {
    H.main.set('002636', H.make())
    renderChart('002636')

    // 图表壳(周期切换器)已挂载 + 显式加载态 —— 不需要等任何数据
    expect(screen.getByText('日K')).toBeTruthy()
    expect(screen.getByText('加载中…')).toBeTruthy()
  })

  it('主数据失败 → 显式错误态(不假报有数据, 也不伪装成「无数据」)', async () => {
    const main = H.make()
    H.main.set('002636', main)
    H.summary.current = H.make()

    renderChart('002636')
    await act(async () => {
      main.reject(new Error('klines-down'))
    })

    await waitFor(() => expect(screen.getByText('错误: klines-down')).toBeTruthy())
    expect(screen.queryByText(/根K线/)).toBeNull()
    expect(screen.queryByText('无数据')).toBeNull()
  })

  it('symbol 切换竞态守卫: 旧标的迟到响应不覆盖新标的', async () => {
    const a = H.make()
    const b = H.make()
    H.main.set('002636', a)
    H.main.set('002661', b)
    H.summary.current = H.make()

    const view = renderChart('002636')
    view.rerender(
      <KlineChart symbol="002661" market="CN" initialInterval="1d" initialDays={120} height={420} />,
    )

    await act(async () => {
      b.resolve({ klines: bars(5) })
    })
    await waitFor(() => expect(screen.getByText('5 根K线')).toBeTruthy())

    // 旧标的(002636)的迟到响应必须被丢弃 —— 屏上仍是新标的的 5 根
    await act(async () => {
      a.resolve({ klines: bars(3) })
    })
    expect(screen.getByText('5 根K线')).toBeTruthy()
    expect(screen.queryByText('3 根K线')).toBeNull()
  })
})
