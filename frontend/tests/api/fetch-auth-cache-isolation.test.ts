// @vitest-environment jsdom
//
// 安全回归钉(2026-11-07): **认证数据缓存必须按登录身份隔离**。
//
// 故障形态: `fetchAPI` 的 30s 响应缓存与在途合并(single-flight) 用 `token.slice(0, 8)` 当 key 前缀,
// 而 HS256 的 JWT 头部(`{"alg":"HS256","typ":"JWT"}`)对所有用户完全相同 —— 前 8 字符恒为
// `eyJhbGci`。于是同一标签页内 A 登录后再换 B, B 的个人中心/头像/统计会直接命中 A 的缓存条目。
//
// 本测试走**真** `fetchAPI`(只 stub `globalThis.fetch`), 钉住五条契约:
//   1. A/B 两个账号(HS256 前缀相同)的 GET 响应与在途请求**互不命中**;
//   2. 同一身份内缓存/合并语义不变(隔离没有把缓存关掉);
//   3. 无 localStorage token(Cookie-only / 匿名)时身份不可信 ⇒ **禁用**响应缓存与在途合并,
//      且不发 Authorization 头;
//   4. 登出同步清缓存并广播身份切换事件; 旧身份的**迟到响应**不得污染新身份的缓存;
//   5. token 不进日志、不进事件 detail(`getAuthIdentity` 只暴露 claim)。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  AUTH_IDENTITY_EVENT,
  clearResponseCache,
  fetchAPI,
  getAuthIdentity,
  logout,
  notifyAuthIdentityChanged,
} from '@sida/api'
import { AUTH_CHANGED_EVENT } from '@/hooks/useApiQuery'

it('登录事件与身份订阅使用同一名称', () => {
  expect(AUTH_IDENTITY_EVENT).toBe(AUTH_CHANGED_EVENT)
})

/** base64url(无填充) —— 与真实 JWT 段编码一致。 */
function b64url(value: unknown): string {
  return btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** 后端同款 HS256 JWT(头部对所有用户相同 —— 正是本次故障的根因)。 */
function tokenFor(sub: string, username: string, jti = 'jti-1'): string {
  const header = b64url({ alg: 'HS256', typ: 'JWT' })
  const payload = b64url({ sub, username, role: 'member', jti, aud: 'user' })
  return `${header}.${payload}.sig-${sub}-${jti}`
}

const TOKEN_A = tokenFor('id-alice', 'alice')
const TOKEN_B = tokenFor('id-bob', 'bob')

/** 与登录页 `saveToken` 等价: 写本地凭证 + 同步通知身份切换。 */
function loginAs(token: string): void {
  localStorage.setItem('token', token)
  localStorage.setItem('token_expires', new Date(Date.now() + 3_600_000).toISOString())
  notifyAuthIdentityChanged('login')
}

/** 回显请求身份的最小 Response 替身(fetchAPI 只读 status / json())。 */
function echoingResponse(headers: Record<string, string> | undefined): Response {
  const who = headers?.Authorization ?? headers?.authorization ?? null
  return { status: 200, json: async () => ({ code: 0, data: { who }, message: '' }) } as unknown as Response
}

/** 可手动放行的 Response —— 把"在途"窗口拉长, 用于迟到响应/并发合并断言。 */
function deferredResponse() {
  let release!: (r: Response) => void
  const promise = new Promise<Response>((res) => {
    release = res
  })
  return { promise, release }
}

beforeEach(() => {
  localStorage.clear()
  clearResponseCache()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  clearResponseCache()
})

describe('fetchAPI 缓存按认证身份隔离', () => {
  it('A/B 同 HS256 前缀也不共用缓存(修复前 B 会读到 A 的响应)', async () => {
    // 前提钉死: 旧实现的分区键(token 前 8 字符)对两个账号完全相同
    expect(TOKEN_A.slice(0, 8)).toBe(TOKEN_B.slice(0, 8))

    const fetchMock = vi.fn(async (_url: unknown, opts?: RequestInit) =>
      echoingResponse(opts?.headers as Record<string, string> | undefined),
    )
    vi.stubGlobal('fetch', fetchMock)

    loginAs(TOKEN_A)
    const a = await fetchAPI<{ who: string | null }>('/profile')
    expect(a.who).toBe(`Bearer ${TOKEN_A}`)

    // 换号(登录页同标签页 SPA 切换, 不刷新页面)
    loginAs(TOKEN_B)
    const b = await fetchAPI<{ who: string | null }>('/profile')

    expect(b.who).toBe(`Bearer ${TOKEN_B}`) // 修复前 = A 的 token(缓存命中)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('同一身份内缓存与在途合并语义不变(隔离没有关掉缓存)', async () => {
    const gate = deferredResponse()
    const fetchMock = vi.fn(() => gate.promise)
    vi.stubGlobal('fetch', fetchMock)

    loginAs(TOKEN_A)
    const p1 = fetchAPI('/market/phase')
    const p2 = fetchAPI('/market/phase')
    expect(fetchMock).toHaveBeenCalledTimes(1) // 同身份同键仍在途合并

    gate.release({ status: 200, json: async () => ({ code: 0, data: { n: 1 }, message: '' }) } as unknown as Response)
    await expect(p1).resolves.toEqual({ n: 1 })
    await expect(p2).resolves.toEqual({ n: 1 })

    await fetchAPI('/market/phase')
    expect(fetchMock).toHaveBeenCalledTimes(1) // 30s 响应缓存照旧命中
  })

  it('身份未经通知就变了(别的标签页/别的模块换 token): 仍按 claim 分区, 不串号', async () => {
    const fetchMock = vi.fn(async (_url: unknown, opts?: RequestInit) =>
      echoingResponse(opts?.headers as Record<string, string> | undefined),
    )
    vi.stubGlobal('fetch', fetchMock)

    loginAs(TOKEN_A)
    const a = await fetchAPI<{ who: string | null }>('/profile')
    expect(a.who).toBe(`Bearer ${TOKEN_A}`)

    // 关键: 直接换 token, **不**走 notifyAuthIdentityChanged —— 此时没有任何缓存清理,
    // 唯一防线就是缓存键里的 claim 身份(token 前缀在这里完全撞车)。
    localStorage.setItem('token', TOKEN_B)
    const b = await fetchAPI<{ who: string | null }>('/profile')
    expect(b.who).toBe(`Bearer ${TOKEN_B}`)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('Cookie-only(无 localStorage token): 身份不可信 ⇒ 禁用缓存与合并, 且不带 Authorization', async () => {
    const fetchMock = vi.fn(async (_url: unknown, opts?: RequestInit) =>
      echoingResponse(opts?.headers as Record<string, string> | undefined),
    )
    vi.stubGlobal('fetch', fetchMock)

    expect(getAuthIdentity()).toBeNull() // Cookie-only 会话拿不到可信身份

    const r1 = await fetchAPI<{ who: string | null }>('/profile')
    await fetchAPI('/profile')
    // 两次都必须真发: 共享缓存会把不同 Cookie 账号的响应混在一起
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(r1.who).toBeNull() // 没登录态时不发 Bearer 头(靠 Cookie)
    expect((fetchMock.mock.calls[0][1] as RequestInit).credentials).toBe('include')

    // 在途合并同样禁用: 并发两发 = 两条真实请求
    const gate = deferredResponse()
    const gatedFetch = vi.fn(() => gate.promise)
    vi.stubGlobal('fetch', gatedFetch)
    const c1 = fetchAPI('/market/phase')
    const c2 = fetchAPI('/market/phase')
    expect(gatedFetch).toHaveBeenCalledTimes(2)
    gate.release({ status: 200, json: async () => ({ code: 0, data: { n: 2 }, message: '' }) } as unknown as Response)
    await Promise.all([c1, c2])
  })

  it('旧身份的在途请求: 不共享给新身份, 迟到响应也不污染新缓存', async () => {
    const gateA = deferredResponse()
    const fetchMock = vi.fn((_url: unknown, opts?: RequestInit) => {
      const auth = (opts?.headers as Record<string, string> | undefined)?.Authorization
      // A 的首发挂在 gate 上; 之后(B 身份)立即返回
      if (auth === `Bearer ${TOKEN_A}` && fetchMock.mock.calls.length === 1) return gateA.promise
      return Promise.resolve(echoingResponse(opts?.headers as Record<string, string> | undefined))
    })
    vi.stubGlobal('fetch', fetchMock)

    loginAs(TOKEN_A)
    const pendingA = fetchAPI<{ who: string | null }>('/profile') // A 的在途请求(未回)

    // 登录 B: 身份切换必须同步清掉 A 的在途表/响应缓存
    loginAs(TOKEN_B)
    const b = await fetchAPI<{ who: string | null }>('/profile')
    expect(b.who).toBe(`Bearer ${TOKEN_B}`)
    expect(fetchMock).toHaveBeenCalledTimes(2) // 没有复用 A 的在途 promise

    // A 的响应现在才到 —— 必须被丢弃(代次已推进), 不能写进 B 的缓存
    gateA.release(
      { status: 200, json: async () => ({ code: 0, data: { who: `Bearer ${TOKEN_A}` }, message: '' }) } as unknown as Response,
    )
    await expect(pendingA).resolves.toEqual({ who: `Bearer ${TOKEN_A}` }) // 原调用方拿到自己的结果

    // B 再读: 命中 B 自己的缓存, 绝不能拿到 A 的迟到响应
    const b2 = await fetchAPI<{ who: string | null }>('/profile')
    expect(b2.who).toBe(`Bearer ${TOKEN_B}`)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('登出: 清缓存 + 移本地凭证 + 同步广播身份切换(不带凭证)', async () => {
    const fetchMock = vi.fn(async (_url: unknown, opts?: RequestInit) =>
      echoingResponse(opts?.headers as Record<string, string> | undefined),
    )
    vi.stubGlobal('fetch', fetchMock)

    const events: Array<{ type: string; detail: unknown }> = []
    const onIdentity = (e: Event) => events.push({ type: e.type, detail: (e as CustomEvent).detail })
    window.addEventListener(AUTH_IDENTITY_EVENT, onIdentity)

    loginAs(TOKEN_A)
    await fetchAPI('/profile')
    expect(fetchMock).toHaveBeenCalledTimes(1)

    events.length = 0 // 只看登出这一次的广播
    logout()

    window.removeEventListener(AUTH_IDENTITY_EVENT, onIdentity)
    expect(localStorage.getItem('token')).toBeNull()
    expect(events.map((e) => e.detail)).toEqual([{ reason: 'logout' }])
    expect(fetchMock).toHaveBeenCalledTimes(2) // best-effort POST /auth/logout
    expect(getAuthIdentity()).toBeNull()

    // 登出后同路径必须重新取数(A 的缓存已清, 且 Cookie-only 不再缓存)
    await fetchAPI('/profile')
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('身份标识与日志都不含 token', async () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined))
    const seenDetails: unknown[] = []
    const onIdentity = (e: Event) => seenDetails.push((e as CustomEvent).detail)
    window.addEventListener(AUTH_IDENTITY_EVENT, onIdentity)

    const fetchMock = vi.fn(async (_url: unknown, opts?: RequestInit) =>
      echoingResponse(opts?.headers as Record<string, string> | undefined),
    )
    vi.stubGlobal('fetch', fetchMock)

    loginAs(TOKEN_A)
    const identity = getAuthIdentity()
    await fetchAPI('/profile')
    await fetchAPI('/orders')
    notifyAuthIdentityChanged('manual')

    window.removeEventListener(AUTH_IDENTITY_EVENT, onIdentity)

    // 身份只用 JWT claim(sub/username), 不是 token 前缀也不是 token 本身
    expect(identity).toBe('u:id-alice')
    const payloadSeg = TOKEN_A.split('.')[1]
    const logged = spies.flatMap((s) => s.mock.calls.map((args) => args.map((a) => String(a)).join(' ')))
    for (const line of logged) {
      expect(line).not.toContain(TOKEN_A)
      expect(line).not.toContain(payloadSeg)
    }
    expect(JSON.stringify(seenDetails)).not.toContain(payloadSeg)
    expect(seenDetails).toEqual([{ reason: 'login' }, { reason: 'manual' }])
  })
})
