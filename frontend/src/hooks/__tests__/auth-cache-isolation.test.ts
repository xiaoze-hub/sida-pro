// @vitest-environment jsdom
/**
 * 跨账号缓存 / 401 隔离(2026-11-07 复审) —— **真实 fetchAPI + 打桩 fetch**, 不 mock @sida/api。
 *
 * 已证实的故障面:
 *  ① A 的在途请求在 B 登录成功后才收到 401 ⇒ 旧实现无条件 `logout()`, 把**刚登录的 B 踢走**;
 *  ② JWT `sub` 才是稳定身份, `username` 可重名/可改 ⇒ 同名不同 sub 的两个人分到同一桶;
 *  ③ 同一个人撤销会话后重登(新 token、同 sub): 旧缓存/在途响应被当成"同一个我"继续复用;
 *  ④ 无可读凭证(Cookie-only / 匿名)时不能共享缓存, 否则等于赌"两个调用方同一账号";
 *  ⑤ 分桶串里绝不能出现 token 原文(会随 query key 进 devtools/错误上报)。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  clearResponseCache,
  fetchAPI,
  getAuthGeneration,
  getAuthScope,
  notifyAuthIdentityChanged,
  type ApiError,
} from '@sida/api'

const A_TOKEN = tokenWith({ username: 'alice', sub: 'user-a', jti: 'a1' })
const A_TOKEN_2 = tokenWith({ username: 'alice', sub: 'user-a', jti: 'a2' }) // 同 sub, 新发 token
const B_TOKEN = tokenWith({ username: 'alice', sub: 'user-b', jti: 'b1' }) // 同名, 不同人

function tokenWith(claims: Record<string, unknown>): string {
  return `e30.${btoa(JSON.stringify(claims))}.sig`
}

function ok(data: unknown): Response {
  return {
    status: 200,
    headers: { get: () => null },
    json: async () => ({ code: 0, data, message: '' }),
  } as unknown as Response
}

function unauthorized(): Response {
  return {
    status: 401,
    headers: { get: () => null },
    json: async () => ({ code: 401, data: null, message: 'unauthorized' }),
  } as unknown as Response
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => {
    resolve = res
  })
  return { promise, resolve }
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  localStorage.clear()
  clearResponseCache()
  // 重置登出单飞锁(上一用例可能触发过 logout)
  notifyAuthIdentityChanged('login')
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  clearResponseCache()
})

describe('身份分桶: sub 优先 + 代次', () => {
  it('同名不同 sub 是两个分桶; 且分桶串里没有 token 原文', () => {
    localStorage.setItem('token', A_TOKEN)
    const a = getAuthScope()
    localStorage.setItem('token', B_TOKEN)
    const b = getAuthScope()
    expect(a).toMatch(/^u:user-a@\d+$/)
    expect(b).toMatch(/^u:user-b@\d+$/)
    expect(a).not.toBe(b)
    for (const scope of [a, b]) {
      expect(scope).not.toContain(A_TOKEN.slice(-12))
      expect(scope).not.toContain(B_TOKEN.slice(-12))
      expect(scope).not.toContain(A_TOKEN.split('.')[1])
    }
  })

  it('同 sub 换发 token: 代次必须推进(否则旧会话时代的响应还挂在新会话上)', () => {
    localStorage.setItem('token', A_TOKEN)
    const gen1 = getAuthGeneration()
    const scope1 = getAuthScope()
    localStorage.setItem('token', A_TOKEN_2)
    expect(getAuthGeneration()).toBeGreaterThan(gen1)
    expect(getAuthScope()).not.toBe(scope1)
  })

  it('同 sub 换发 token(静默替换, 无任何事件): 旧缓存不再命中, 必须重新取数', async () => {
    localStorage.setItem('token', A_TOKEN)
    fetchMock.mockResolvedValueOnce(ok({ avatar: 'OLD' }))
    await expect(fetchAPI('/profile')).resolves.toEqual({ avatar: 'OLD' })
    expect(fetchMock).toHaveBeenCalledTimes(1)

    // 直接换 token(模拟续期/重登), 不广播任何事件
    localStorage.setItem('token', A_TOKEN_2)
    fetchMock.mockResolvedValueOnce(ok({ avatar: 'NEW' }))
    await expect(fetchAPI('/profile')).resolves.toEqual({ avatar: 'NEW' })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('同 sub 换发 token: 旧 token 发起的在途响应不得写进新代次缓存', async () => {
    localStorage.setItem('token', A_TOKEN)
    const slow = deferred<Response>()
    fetchMock.mockReturnValueOnce(slow.promise)
    const oldRequest = fetchAPI<{ avatar: string }>('/profile')

    localStorage.setItem('token', A_TOKEN_2) // 换代, 旧请求仍在途

    fetchMock.mockResolvedValueOnce(ok({ avatar: 'NEW' }))
    await expect(fetchAPI('/profile')).resolves.toEqual({ avatar: 'NEW' })

    // 旧响应现在才到: 返回给原调用方可以, 但**不许**污染新代次的缓存
    slow.resolve(ok({ avatar: 'OLD' }))
    await expect(oldRequest).resolves.toEqual({ avatar: 'OLD' })

    // 立刻再读一次: 命中的必须是新代次的结果(OLD 那次写入已被丢弃, 没有覆盖缓存)
    await expect(fetchAPI('/profile')).resolves.toEqual({ avatar: 'NEW' })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

describe('两账号: 同一个 SPA 里换号绝不能串数据', () => {
  it('A 的缓存不会命中给 B(即使 username 相同)', async () => {
    localStorage.setItem('token', A_TOKEN)
    fetchMock.mockResolvedValueOnce(ok({ nickname: 'A', avatar: 'AVATAR-A' }))
    await expect(fetchAPI('/profile')).resolves.toEqual({ nickname: 'A', avatar: 'AVATAR-A' })

    localStorage.setItem('token', B_TOKEN)
    fetchMock.mockResolvedValueOnce(ok({ nickname: 'B', avatar: 'AVATAR-B' }))
    await expect(fetchAPI('/profile')).resolves.toEqual({ nickname: 'B', avatar: 'AVATAR-B' })
    expect(fetchMock).toHaveBeenCalledTimes(2)

    // 请求头带的是当前账号的凭证
    const headers = (fetchMock.mock.calls[1][1] as RequestInit).headers as Record<string, string>
    expect(headers.Authorization).toBe(`Bearer ${B_TOKEN}`)
  })

  it('A 的在途请求不会与 B 的请求合并(在途表按身份分桶)', async () => {
    localStorage.setItem('token', A_TOKEN)
    const aPending = deferred<Response>()
    fetchMock.mockReturnValueOnce(aPending.promise)
    const aReq = fetchAPI<{ who: string }>('/profile')

    localStorage.setItem('token', B_TOKEN)
    fetchMock.mockResolvedValueOnce(ok({ who: 'B' }))
    const bReq = fetchAPI<{ who: string }>('/profile')

    await expect(bReq).resolves.toEqual({ who: 'B' })
    aPending.resolve(ok({ who: 'A' }))
    await expect(aReq).resolves.toEqual({ who: 'A' })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('无凭证(Cookie-only / 匿名): 不读缓存、不写缓存、不合并', async () => {
    fetchMock.mockResolvedValueOnce(ok({ n: 1 }))
    await expect(fetchAPI('/profile')).resolves.toEqual({ n: 1 })
    fetchMock.mockResolvedValueOnce(ok({ n: 2 }))
    await expect(fetchAPI('/profile')).resolves.toEqual({ n: 2 })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(getAuthScope()).toBeNull()
  })
})

describe('401: 旧会话的迟到 401 不能登出新账号', () => {
  it('A 发出的请求在 B 登录后返回 401 ⇒ 只让该请求失败, B 的会话与缓存都不动', async () => {
    localStorage.setItem('token', A_TOKEN)
    const slow = deferred<Response>()
    fetchMock.mockReturnValueOnce(slow.promise)
    const oldRequest = fetchAPI('/profile')

    // 用户换号: B 已登录并写入新 token
    localStorage.setItem('token', B_TOKEN)

    slow.resolve(unauthorized())
    const err = (await oldRequest.catch(e => e)) as ApiError
    expect(err.status).toBe(401)
    expect(err.staleAuth).toBe(true)

    // B 仍然是登录状态(旧实现会把这里清空并跳登录页)
    expect(localStorage.getItem('token')).toBe(B_TOKEN)

    // B 的请求照常带 B 的凭证并拿到数据
    fetchMock.mockResolvedValueOnce(ok({ nickname: 'B' }))
    await expect(fetchAPI('/profile')).resolves.toEqual({ nickname: 'B' })
    const headers = (fetchMock.mock.calls[1][1] as RequestInit).headers as Record<string, string>
    expect(headers.Authorization).toBe(`Bearer ${B_TOKEN}`)
  })

  it('同 sub 换发 token 后, 旧 token 的 401 同样不算当前会话失效', async () => {
    localStorage.setItem('token', A_TOKEN)
    const slow = deferred<Response>()
    fetchMock.mockReturnValueOnce(slow.promise)
    const oldRequest = fetchAPI('/profile')
    localStorage.setItem('token', A_TOKEN_2)

    slow.resolve(unauthorized())
    const err = (await oldRequest.catch(e => e)) as ApiError
    expect(err.staleAuth).toBe(true)
    expect(localStorage.getItem('token')).toBe(A_TOKEN_2)
  })

  it('当前凭证的 401 仍然登出(控制组: 不能把真失效当成"迟到")', async () => {
    localStorage.setItem('token', A_TOKEN)
    fetchMock.mockResolvedValueOnce(unauthorized())
    const err = (await fetchAPI('/profile').catch(e => e)) as ApiError
    expect(err.status).toBe(401)
    expect(err.staleAuth).toBeUndefined()
    expect(localStorage.getItem('token')).toBeNull()
  })
})
