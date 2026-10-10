// @vitest-environment jsdom
//
// perf(2026-10-02) 回归钉: `fetchAPI` 的**在途同键合并**(single-flight)。
//
// 背景(B1 首页冷态走查代码定位): 首页「市场温度」被 3 个组件各打一次 `/market/phase`
// (`usePhaseLabel` / `MarketPhaseCard` / `PhaseGaugeCard`), 「主线 Top1」被 2 个组件各打一次
// `/market/mainline`。fetchAPI 的 30s 缓存只在**响应回来之后**才生效, 冷启动那一刻 3~5 个
// 同键请求会一起真发出去, 与首屏其余 15+ 请求争抢同域连接池/后端单 worker 队列。
//
// 本测试走**真** fetchAPI(只 stub `globalThis.fetch` 数真实请求次数), 钉住四条契约:
//   1. 并发同键 GET → 只发 1 次真实请求, 各调用方拿到同一份 data;
//   2. 合并只在“在途”窗口内 —— 响应回来后仍回落 30s 响应缓存语义;
//   3. `cacheMode:'reload' | false` 与自带 `signal` 一律**不合并**(实时端点/手动刷新语义不变);
//   4. 失败广播给所有调用方, 并且**下一次调用会重试**(在途表已清理, 不会永久卡住)。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearResponseCache, fetchAPI } from '@panwatch/api'

/** 最小 Response 替身(fetchAPI 只读 status / json())。 */
function okResponse(data: unknown): Response {
  return { status: 200, json: async () => ({ code: 0, data, message: '' }) } as unknown as Response
}

/** 可手动放行的 Response —— 用于把“请求在途”这段窗口拉长。 */
function deferredResponse() {
  let release!: (r: Response) => void
  const promise = new Promise<Response>((res) => {
    release = res
  })
  return { promise, release }
}

beforeEach(() => {
  clearResponseCache()
})
afterEach(() => {
  vi.unstubAllGlobals()
  clearResponseCache()
})

describe('fetchAPI 在途同键合并(single-flight)', () => {
  it('并发同键 GET 只发一次真实请求, 各调用方拿到同一份 data', async () => {
    const gate = deferredResponse()
    const fetchMock = vi.fn(() => gate.promise)
    vi.stubGlobal('fetch', fetchMock)

    const p1 = fetchAPI<{ n: number }>('/market/phase')
    const p2 = fetchAPI<{ n: number }>('/market/phase')
    const p3 = fetchAPI<{ n: number }>('/market/phase')

    // 修复前 = 3(三次真发); 现在同键在途共享 1 次
    expect(fetchMock).toHaveBeenCalledTimes(1)

    gate.release(okResponse({ n: 7 }))
    await expect(p1).resolves.toEqual({ n: 7 })
    await expect(p2).resolves.toEqual({ n: 7 })
    await expect(p3).resolves.toEqual({ n: 7 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('路径不同 / 方法不同 → 不合并', async () => {
    const fetchMock = vi.fn(async () => okResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await Promise.all([
      fetchAPI('/market/phase'),
      fetchAPI('/market/mainline'),
      fetchAPI('/market/phase', { method: 'POST' }),
    ])
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('响应回来后回落 30s 响应缓存: 再调一次仍不发请求(合并未改变缓存语义)', async () => {
    const fetchMock = vi.fn(async () => okResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await fetchAPI('/market/phase')
    await fetchAPI('/market/phase')
    expect(fetchMock).toHaveBeenCalledTimes(1)

    // 强制要新数据 → 跳过缓存与合并, 真发一次
    await fetchAPI('/market/phase', { cacheMode: 'reload' })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('cacheMode:reload / cacheMode:false 不合并(实时端点与手动刷新语义不变)', async () => {
    const gate = deferredResponse()
    const fetchMock = vi.fn(() => gate.promise)
    vi.stubGlobal('fetch', fetchMock)

    const a = fetchAPI('/realtime/x', { cacheMode: 'reload' })
    const b = fetchAPI('/realtime/x', { cacheMode: 'reload' })
    const c = fetchAPI('/realtime/y', { cacheMode: false })
    const d = fetchAPI('/realtime/y', { cacheMode: false })
    expect(fetchMock).toHaveBeenCalledTimes(4)

    gate.release(okResponse({ ok: true }))
    await Promise.all([a, b, c, d])
  })

  it('自带 signal 不合并(取消权归调用方, 不能被共享)', async () => {
    const fetchMock = vi.fn(async () => okResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    const s1 = new AbortController().signal
    const s2 = new AbortController().signal
    await Promise.all([fetchAPI('/x', { signal: s1 }), fetchAPI('/x', { signal: s2 })])
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('失败广播给所有调用方, 且下一次调用会重试(在途表已清理, 不会永久卡死)', async () => {
    const failMock = vi.fn(async () => {
      throw new Error('network-down')
    })
    vi.stubGlobal('fetch', failMock)

    const r1 = fetchAPI('/market/phase')
    const r2 = fetchAPI('/market/phase')
    await expect(r1).rejects.toThrow('network-down')
    await expect(r2).rejects.toThrow('network-down')
    // 2026-10-10 加载韧性: GET 幂等请求失败会**自动重试一次** —— 同一份在途 promise 内共发
    // 2 次真实请求(首次 + 重试), 两个调用方仍共享**同一份结果**(而不是各自再发一轮)。
    expect(failMock).toHaveBeenCalledTimes(2)

    // 失败不写响应缓存 → 恢复后下一次调用必须重新真发
    const okMock = vi.fn(async () => okResponse({ n: 1 }))
    vi.stubGlobal('fetch', okMock)
    await expect(fetchAPI('/market/phase')).resolves.toEqual({ n: 1 })
    expect(okMock).toHaveBeenCalledTimes(1)
  })
})
