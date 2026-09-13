// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearResponseCache, fetchAPI, insightApi } from '@panwatch/api'

/**
 * 复审 Finding 2 的**回归护栏**: `/orderbook-ob` 与 `/seal-quality` 是**实时**端点, 必须默认
 * 跳过 `fetchAPI` 的 30s GET 内存缓存。
 *
 * 不这么做时(修复前的实际行为): ① 工作台「盘口资金」标签的「刷新」按钮在 30s 内是**空操作**
 * (而头部 `updatedAt` 仍在变 = 假的"已刷新"信号); ② 该标签的 30s 轮询 tick 落在上一次轮询写下
 * 的缓存 30s 窗口内 ⇒ 缓存命中 ⇒ 真实取数退化成 ~60s。同端点兄弟组件 `OrderBookObBar`
 * 正因此显式传了 `cacheMode:'reload'`。
 *
 * 判据走**真** `fetchAPI`(不 mock `@panwatch/api`), 只 stub `globalThis.fetch` 数真实请求次数;
 * 对照组 `fetchAPI('/some-cached-endpoint')`(普通 GET)必须**保持**原缓存行为 —— 证明本修复
 * 没有扩大到其他端点。
 */

/** 最小 Response 替身(`fetchAPI` 只读 `status` 与 `json()`)。 */
function okResponse() {
  return {
    status: 200,
    json: async () => ({ code: 0, data: { ok: true }, message: '' }),
  } as unknown as Response
}

describe('insightApi 实时端点默认跳过 30s GET 缓存', () => {
  const fetchMock = vi.fn(async () => okResponse())

  beforeEach(() => {
    clearResponseCache()
    fetchMock.mockClear()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    clearResponseCache()
  })

  it('orderbookOb / sealQuality 每次都真发请求; 普通 GET 端点照旧命中缓存', async () => {
    await insightApi.orderbookOb('002636')
    await insightApi.orderbookOb('002636')
    expect(fetchMock).toHaveBeenCalledTimes(2) // 修复前 = 1(第二次命中 30s 缓存)

    await insightApi.sealQuality('002636')
    await insightApi.sealQuality('002636')
    expect(fetchMock).toHaveBeenCalledTimes(4) // 修复前 = 2

    // 对照组: 非实时端点缓存行为未变(第二次命中缓存, 不发请求)
    await fetchAPI('/some-cached-endpoint')
    await fetchAPI('/some-cached-endpoint')
    expect(fetchMock).toHaveBeenCalledTimes(5)
  })

  it('调用方仍可用 cacheMode:false / 数字 TTL 覆盖默认的 reload', async () => {
    await insightApi.orderbookOb('002636', { cacheMode: false })
    await insightApi.orderbookOb('002636', { cacheMode: false })
    expect(fetchMock).toHaveBeenCalledTimes(2) // false ⇒ 不读也不写缓存

    await insightApi.sealQuality('002636', { cacheMode: 60 })
    await insightApi.sealQuality('002636', { cacheMode: 60 })
    expect(fetchMock).toHaveBeenCalledTimes(3) // 数字 TTL 只覆盖默认值, 行为仍走 fetchAPI 原逻辑
  })
})
