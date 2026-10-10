// @vitest-environment jsdom
//
// 前端加载韧性(2026-10-10)回归钉: 请求超时 + GET 幂等重试。用户报障「页面要加载很久,
// 需要刷新浏览器才能显示」—— 根因之一是后端偶发 502 闪断/慢时 fetch **无超时**, 组件永运 loading。
//
// 本测试走**真** fetchAPI(只 stub `globalThis.fetch`), 钉住四条契约(禁真实网络):
//   1. 请求超时抛**类型化** `ApiTimeoutError`(kind=TIMEOUT, 带实际超时毫秒);
//   2. GET 幂等请求失败(网络错误 / 超时 / 5xx)自动**重试一次**, 重试成功即返回;
//   3. 重试只一次(共 2 次真实请求), 仍失败则抛原错误(带 kind=NETWORK), 不无限重试;
//   4. POST 失败**不重试**(可能已产生副作用); 4xx 是确定性错误也不重试;
//      调用方自带 `signal` 时取消权归调用方, 原样抛(不误报超时/不重试)。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiTimeoutError, clearResponseCache, fetchAPI } from '@panwatch/api'

/** 最小 Response 替身(fetchAPI 只读 status / json())。 */
function okResponse(data: unknown): Response {
  return { status: 200, json: async () => ({ code: 0, data, message: '' }) } as unknown as Response
}
function statusResponse(status: number): Response {
  return {
    status,
    json: async () => ({ code: status, data: null, message: `HTTP ${status}` }),
  } as unknown as Response
}

/** 永不 resolve 的 fetch, 只在 signal abort 时 reject AbortError —— 模拟"后端不响应"。 */
function hangingFetch() {
  return vi.fn(
    (_url: RequestInfo | URL, opts?: RequestInit) =>
      new Promise<Response>((_res, rej) => {
        const signal = opts?.signal
        if (signal) {
          signal.addEventListener('abort', () => {
            const e = new Error('aborted')
            e.name = 'AbortError'
            rej(e)
          })
        }
      }),
  )
}

beforeEach(() => {
  clearResponseCache()
})
afterEach(() => {
  vi.unstubAllGlobals()
  clearResponseCache()
})

describe('fetchAPI 超时(类型化错误)', () => {
  it('POST 超时抛 ApiTimeoutError(kind=TIMEOUT, 带 timeoutMs), 且不重试(仅 1 次)', async () => {
    const f = hangingFetch()
    vi.stubGlobal('fetch', f)

    const err = (await fetchAPI('/slow-post', { method: 'POST', timeoutMs: 15 }).catch((e) => e)) as ApiTimeoutError
    expect(err).toBeInstanceOf(ApiTimeoutError)
    expect(err.name).toBe('ApiTimeoutError')
    expect(err.kind).toBe('TIMEOUT')
    expect(err.timeoutMs).toBe(15)
    expect(err.message).toBe('请求超时，请稍后重试')
    expect(f).toHaveBeenCalledTimes(1) // POST 永不重试
  })

  it('GET 超时 → 自动重试一次(共 2 次), 仍失败抛 ApiTimeoutError', async () => {
    const f = hangingFetch()
    vi.stubGlobal('fetch', f)

    const err = (await fetchAPI('/slow-get', { timeoutMs: 15 }).catch((e) => e)) as ApiTimeoutError
    expect(err).toBeInstanceOf(ApiTimeoutError)
    expect(err.kind).toBe('TIMEOUT')
    expect(f).toHaveBeenCalledTimes(2) // 首次 + 重试一次
  })
})

describe('fetchAPI GET 幂等重试', () => {
  it('网络失败自动重试一次: 首次失败、重试成功即返回', async () => {
    const f = vi
      .fn()
      .mockRejectedValueOnce(new Error('Failed to fetch'))
      .mockResolvedValueOnce(okResponse({ n: 1 }))
    vi.stubGlobal('fetch', f)

    await expect(fetchAPI('/retry-ok')).resolves.toEqual({ n: 1 })
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('连续失败只重试一次(共 2 次), 抛原错误且带 kind=NETWORK', async () => {
    const f = vi.fn(async () => {
      throw new Error('network-down')
    })
    vi.stubGlobal('fetch', f)

    const err = (await fetchAPI('/retry-exhausted').catch((e) => e)) as Error & { kind?: string }
    expect(err.message).toBe('network-down') // 保留原始 message
    expect(err.kind).toBe('NETWORK')
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('GET 5xx 视为瞬时故障重试一次: 第二次成功即返回', async () => {
    const f = vi.fn().mockResolvedValueOnce(statusResponse(503)).mockResolvedValueOnce(okResponse({ ok: true }))
    vi.stubGlobal('fetch', f)

    await expect(fetchAPI('/retry-5xx')).resolves.toEqual({ ok: true })
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('GET 4xx 是确定性错误 → 不重试(仅 1 次), 错误带 status + kind', async () => {
    const f = vi.fn(async () => statusResponse(404))
    vi.stubGlobal('fetch', f)

    const err = (await fetchAPI('/not-found').catch((e) => e)) as Error & { status?: number; kind?: string }
    expect(f).toHaveBeenCalledTimes(1)
    expect(err.status).toBe(404)
    expect(err.kind).toBe('HTTP_4xx')
  })
})

describe('fetchAPI POST 不重试 / 调用方 signal 不被重试', () => {
  it('POST 网络失败仅发 1 次(可能已产生副作用, 绝不自动重试)', async () => {
    const f = vi.fn(async () => {
      throw new Error('boom')
    })
    vi.stubGlobal('fetch', f)

    await expect(fetchAPI('/danger', { method: 'POST' })).rejects.toThrow('boom')
    expect(f).toHaveBeenCalledTimes(1)
  })

  it('调用方自带 signal 时取消 → 原样抛 AbortError, 不误报超时/不重试', async () => {
    const ctrl = new AbortController()
    const f = hangingFetch()
    vi.stubGlobal('fetch', f)

    const p = fetchAPI('/caller-cancel', { signal: ctrl.signal })
    ctrl.abort()
    const err = (await p.catch((e) => e)) as Error
    expect(err.name).toBe('AbortError') // 不是 ApiTimeoutError
    expect(err).not.toBeInstanceOf(ApiTimeoutError)
    expect(f).toHaveBeenCalledTimes(1)
  })
})
