// @vitest-environment jsdom
/**
 * B1（2026-10-29 独立复审）· fetchAPI 的**超时只覆盖到响应头**，响应体读不完就永不结束。
 *
 * 真实故障形态：网关/反代先回 `200 OK` 头，chunked 体随后卡住（或只发了一半）。
 * 旧实现把 `AbortController` 计时器放在 `_attemptFetch` 里、`fetch()` resolve（= 收到头）
 * 就 `finally clearTimeout`，于是：
 *   ① `_parseResponse` 的 `res.json()` **无限 pending** ⇒ 本次调用永不 settle；
 *   ② `_INFLIGHT` 里那条共享 promise 永不释放 ⇒ 同键请求永远命中这条僵尸 promise；
 *   ③ 上游 hook 的 in-flight 闸门（`quotesInFlightRef`）也永不释放 ⇒ 报价/市场时钟两把
 *      时钟停在最后一次成功时刻，14:00 仍把 09:31 判成「实时」。
 *
 * 现在的契约：**超时窗口必须覆盖到响应体读完**（头到了不算完成）；
 * 体读到一半被 abort 也抛 `ApiTimeoutError(kind=TIMEOUT)`（不是"解析失败"），
 * 且无论成功失败都要把 `_INFLIGHT` 里那条 promise 摘掉（后续调用重新发请求）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiTimeoutError, clearResponseCache, fetchAPI } from '@sida/api'

/**
 * 响应头立刻到、响应体永远不结束的 fetch。
 *
 * `json()` 挂在 `signal` 上：只有计时器 abort 才会 reject（AbortError），
 * 与浏览器里"体读到一半被 abort"的行为一致。
 */
function headerOnlyHangingBodyFetch() {
  return vi.fn((_url: RequestInfo | URL, opts?: RequestInit) => {
    const res = {
      status: 200,
      headers: { get: () => null },
      json: () =>
        new Promise((_resolve, reject) => {
          const signal = opts?.signal
          const abort = () => {
            const e = new Error('aborted')
            e.name = 'AbortError'
            reject(e)
          }
          if (!signal) return
          if (signal.aborted) abort()
          else signal.addEventListener('abort', abort)
        }),
    } as unknown as Response
    return Promise.resolve(res)
  })
}

/** 正常响应（控制组：证明不是"所有响应都被判超时"）。 */
function okResponse(data: unknown): Response {
  return { status: 200, headers: { get: () => null }, json: async () => ({ code: 0, data, message: '' }) } as unknown as Response
}

beforeEach(() => {
  clearResponseCache()
})

afterEach(() => {
  vi.unstubAllGlobals()
  clearResponseCache()
})

describe('fetchAPI · 超时必须覆盖响应体', () => {
  it('POST 拿到响应头但体挂住 → 仍抛 ApiTimeoutError(kind=TIMEOUT), 不无限 pending', async () => {
    const f = headerOnlyHangingBodyFetch()
    vi.stubGlobal('fetch', f)

    const err = (await fetchAPI('/quotes/batch', { method: 'POST', timeoutMs: 20 }).catch((e) => e)) as ApiTimeoutError
    expect(err).toBeInstanceOf(ApiTimeoutError)
    expect(err.kind).toBe('TIMEOUT')
    expect(err.timeoutMs).toBe(20)
    expect(f).toHaveBeenCalledTimes(1)
  })

  it('挂死的那次失败后，同键请求重新发真实请求（在途合并表已释放）', async () => {
    const hung = headerOnlyHangingBodyFetch()
    vi.stubGlobal('fetch', hung)

    // 第一次：体挂住 ⇒ 超时（GET 幂等重试一次 ⇒ 2 次真实请求）
    const first = (await fetchAPI('/portfolio/summary', { timeoutMs: 20 }).catch((e) => e)) as ApiTimeoutError
    expect(first).toBeInstanceOf(ApiTimeoutError)
    expect(hung).toHaveBeenCalledTimes(2)

    // 第二次：必须重新打网络，而不是复用那条僵尸 promise
    vi.stubGlobal('fetch', vi.fn(async () => okResponse({ ok: true })))
    await expect(fetchAPI('/portfolio/summary', { timeoutMs: 20 })).resolves.toEqual({ ok: true })
  })

  it('并发同键请求共享**一次**真实请求；挂死期间不叠加，超时后各自拿到 TIMEOUT', async () => {
    const f = headerOnlyHangingBodyFetch()
    vi.stubGlobal('fetch', f)

    const a = fetchAPI('/quotes/batch', { method: 'POST', timeoutMs: 20, cacheMode: 'reload' }).catch((e) => e)
    const b = fetchAPI('/quotes/batch', { method: 'POST', timeoutMs: 20, cacheMode: 'reload' }).catch((e) => e)
    const [ra, rb] = await Promise.all([a, b])
    expect(ra).toBeInstanceOf(ApiTimeoutError)
    expect(rb).toBeInstanceOf(ApiTimeoutError)
    // POST 不缓存/不合并 ⇒ 两次真实请求；重点是两次都在超时窗口内 settle（旧实现都不会）
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('体正常返回时不受影响（超时窗口不会误杀正常响应）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => okResponse([{ symbol: '600111' }])))
    await expect(fetchAPI('/quotes/batch', { method: 'POST', timeoutMs: 500 })).resolves.toEqual([
      { symbol: '600111' },
    ])
  })
})
