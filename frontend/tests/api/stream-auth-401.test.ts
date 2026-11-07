// @vitest-environment jsdom
/**
 * 流式入口的 401 语义(2026-11-07 复审) —— 真实 `healthApi.streamSystemStatus` /
 * `chatApi.sendMessageStream`, 只打桩 `fetch`。
 *
 * 已证实的故障面(两个入口都是自己 `fetch` + 自己判 401, 绕开了 fetchAPI 的裁决):
 *  ① A 的流式请求在途 → 用户换号, B 登录成功 → A 的 401 此时才回来 ⇒ 旧实现无条件
 *     `logout()`, 把**刚登录好的 B 踢回登录页**;
 *  ② 控制组不能丢: 当前凭证的 401 必须照旧登出(否则真失效被当成\"迟到\", 用户卡在坏会话里);
 *  ③ 空 token 语义: localStorage 里被写成 `''` / `'null'`(历史版本序列化事故)时, 旧实现仍会发
 *     `Authorization: Bearer null` —— 后端 `_bearer_from_header` 把它当**真凭证**去验签,
 *     验不过就 401, 明明 httpOnly Cookie 有效也被判过期。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  chatApi,
  clearResponseCache,
  healthApi,
  notifyAuthIdentityChanged,
  type ApiError,
} from '@sida/api'

function tokenWith(claims: Record<string, unknown>): string {
  return `e30.${btoa(JSON.stringify(claims))}.sig`
}

const A_TOKEN = tokenWith({ username: 'alice', sub: 'user-a' })
const B_TOKEN = tokenWith({ username: 'alice', sub: 'user-b' })

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => {
    resolve = res
  })
  return { promise, resolve }
}

function unauthorized(): Response {
  return {
    ok: false,
    status: 401,
    headers: { get: () => null },
    body: null,
    json: async () => ({ code: 401, data: null, message: 'unauthorized' }),
  } as unknown as Response
}

/** 一次完整的健康状态流(收到 done 即结束), 用于确认非 401 路径没被改动。 */
function healthStream(): Response {
  const payload = `data: ${JSON.stringify({ nodes: [], done_count: 1, total: 1, checked_at: 'now', done: true })}\n\n`
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload))
      controller.close()
    },
  })
  return { ok: true, status: 200, headers: { get: () => null }, body } as unknown as Response
}

/** 一次完整的聊天流(done 帧), 用于确认非 401 路径没被改动。 */
function chatStream(): Response {
  const payload = `event: done\ndata: ${JSON.stringify({ id: 1, role: 'assistant', content: 'ok', created_at: 'now' })}\n\n`
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload))
      controller.close()
    },
  })
  return { ok: true, status: 200, headers: { get: () => null }, body } as unknown as Response
}

let fetchMock: ReturnType<typeof vi.fn>

function stubFetch(...responses: Array<Response | Promise<Response>>) {
  let call = 0
  fetchMock = vi.fn(async () => {
    const next = responses[Math.min(call, responses.length - 1)]
    call += 1
    return next
  })
  vi.stubGlobal('fetch', fetchMock)
}

function headersOf(callIndex: number): Record<string, string> {
  const init = fetchMock.mock.calls[callIndex]?.[1] as RequestInit | undefined
  return (init?.headers ?? {}) as Record<string, string>
}

beforeEach(() => {
  localStorage.clear()
  clearResponseCache()
  // 复位登出单飞锁(上一用例可能触发过 logout)
  notifyAuthIdentityChanged('login')
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  clearResponseCache()
})

describe('chatApi.sendMessageStream: 401 只处理当前凭证', () => {
  it('Cookie-only 会话换号后旧请求 401 不得清除新身份', async () => {
    const pending = deferred<Response>()
    stubFetch(pending.promise)
    const request = chatApi.sendMessageStream(7, 'q', {})
    localStorage.setItem('user', JSON.stringify({ id: 'user-b' }))
    notifyAuthIdentityChanged('login')
    pending.resolve(unauthorized())
    const err = (await request.catch(e => e)) as ApiError
    expect(err.status).toBe(401)
    expect(localStorage.getItem('user')).toContain('user-b')
  })
  it('A 的流式请求在 B 登录后才 401 ⇒ 不登出 B, 该请求以 staleAuth 失败', async () => {
    localStorage.setItem('token', A_TOKEN)
    const pending = deferred<Response>()
    stubFetch(pending.promise)
    const request = chatApi.sendMessageStream(7, 'q', {})

    // 换号: B 已登录
    localStorage.setItem('token', B_TOKEN)

    pending.resolve(unauthorized())
    const err = (await request.catch(e => e)) as ApiError

    expect(err.status).toBe(401)
    expect(err.staleAuth).toBe(true)
    expect(localStorage.getItem('token')).toBe(B_TOKEN)
  })

  it('当前凭证的 401 仍然登出(控制组)', async () => {
    localStorage.setItem('token', A_TOKEN)
    stubFetch(unauthorized())
    const err = (await chatApi.sendMessageStream(7, 'q', {}).catch(e => e)) as ApiError

    expect(err.status).toBe(401)
    expect(err.staleAuth).toBeUndefined()
    expect(localStorage.getItem('token')).toBeNull()
  })

  it('空/坏 token 不发 Authorization(让 httpOnly Cookie 生效)', async () => {
    localStorage.setItem('token', 'null')
    stubFetch(chatStream())
    await chatApi.sendMessageStream(7, 'q', {})
    expect(headersOf(0).Authorization).toBeUndefined()
  })
})

describe('healthApi.streamSystemStatus: 401 只处理当前凭证', () => {
  it('A 的流式请求在 B 登录后才 401 ⇒ 不登出 B, 该请求以 staleAuth 失败', async () => {
    localStorage.setItem('token', A_TOKEN)
    const pending = deferred<Response>()
    stubFetch(pending.promise)
    const request = healthApi.streamSystemStatus(() => {}, new AbortController().signal)

    localStorage.setItem('token', B_TOKEN)

    pending.resolve(unauthorized())
    const err = (await request.catch(e => e)) as ApiError

    expect(err.status).toBe(401)
    expect(err.staleAuth).toBe(true)
    expect(localStorage.getItem('token')).toBe(B_TOKEN)
  })

  it('当前凭证的 401 仍然登出(控制组)', async () => {
    localStorage.setItem('token', A_TOKEN)
    stubFetch(unauthorized())
    const err = (await healthApi
      .streamSystemStatus(() => {}, new AbortController().signal)
      .catch(e => e)) as ApiError

    expect(err.status).toBe(401)
    expect(err.staleAuth).toBeUndefined()
    expect(localStorage.getItem('token')).toBeNull()
  })

  it('空/坏 token 不发 Authorization(让 httpOnly Cookie 生效)', async () => {
    localStorage.setItem('token', 'null')
    stubFetch(healthStream())
    await healthApi.streamSystemStatus(() => {}, new AbortController().signal)
    expect(headersOf(0).Authorization).toBeUndefined()

    localStorage.setItem('token', '')
    stubFetch(healthStream())
    await healthApi.streamSystemStatus(() => {}, new AbortController().signal)
    expect(headersOf(0).Authorization).toBeUndefined()
  })

  it('正常流不受影响(带 Bearer 且增量派发)', async () => {
    localStorage.setItem('token', A_TOKEN)
    stubFetch(healthStream())
    const seen: unknown[] = []
    await healthApi.streamSystemStatus(update => seen.push(update), new AbortController().signal)
    expect(seen).toHaveLength(1)
    expect(headersOf(0).Authorization).toBe(`Bearer ${A_TOKEN}`)
  })
})
