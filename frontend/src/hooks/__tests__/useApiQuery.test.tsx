// @vitest-environment jsdom
/**
 * useApiQuery 2026-11-07 复审回归:
 *
 * ① **同页切号必须立刻生效**: 登录页换号是 `navigate('/')`(同页 SPA, 不刷新、没有 storage
 *    事件)。只靠 1s 轮询时, 换号后最长 1s 内查询 key 还是旧身份 —— 页面挂着上一个人的资料,
 *    而用户可能就在这一秒里点了保存。统一事件 `sida:auth-changed` 必须在同一帧内改写身份。
 *
 * ② **取消 + 超时不能用 `AbortSignal.any` / `AbortSignal.timeout`**: 老 Safari(<15.4)与
 *    部分内嵌 WebView 没有这两个 API, 一调用就是 TypeError —— 而这是查询的必经路径,
 *    等于全站取数一起挂。这里把两个 API 删掉再跑一遍: 照样取消、照样超时。
 *
 * ③ **超时必须覆盖响应体**: 网关先回 200 头、体卡住。传了自己的 signal 之后 fetchAPI 不再
 *    自建计时器(见 client.ts), 所以计时器必须活到体读完, 否则用户看到的是一直转圈。
 *
 * ④ **不改全站默认语义**: 没显式要求取消的查询一个字都不该变 —— 不传 signal, 让 fetchAPI
 *    自己去做 15s 超时 + GET 单次重试 + 在途合并。
 */
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiTimeoutError, notifyAuthIdentityChanged } from '@sida/api'
import {
  AUTH_CHANGED_EVENT,
  currentIdentity,
  hasTrustedIdentity,
  notifyAuthChanged,
  registerQueryClient,
  scopedKey,
  useApiQuery,
  useIdentityScope,
} from '../useApiQuery'

function tokenOf(name: string): string {
  return `e30.${btoa(JSON.stringify({ username: name, sub: name }))}.sig`
}

/** 造任意 claim 组合的 token: 用来区分"username 同名"与"sub 不同"的两个真实账号。 */
function tokenWith(claims: Record<string, unknown>): string {
  return `e30.${btoa(JSON.stringify(claims))}.sig`
}

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
}

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
}

function okBody(data: unknown): Response {
  return {
    status: 200,
    headers: { get: () => null },
    json: async () => ({ code: 0, data, message: '' }),
  } as unknown as Response
}

/**
 * 头立刻到、体永不结束(只有 signal abort 才 reject)—— 真实故障形态: 反代/网关先回
 * 200 头, chunked 体卡住。与浏览器里"体读到一半被 abort"的行为一致。
 */
function hangingBodyResponse(init?: RequestInit): Response {
  return {
    status: 200,
    headers: { get: () => null },
    json: () =>
      new Promise((_resolve, reject) => {
        const signal = init?.signal
        const abort = () => {
          const err = new Error('aborted')
          err.name = 'AbortError'
          reject(err)
        }
        if (!signal) return
        if (signal.aborted) abort()
        else signal.addEventListener('abort', abort)
      }),
  } as unknown as Response
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  localStorage.clear()
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('登录身份: 统一事件 sida:auth-changed', () => {
  function Probe() {
    const identity = useIdentityScope()
    return <span data-testid="identity">{identity || 'anon'}</span>
  }

  /** 身份分桶串形如 `u:<sub>@<代次>`: 断言主体, 不锁定具体的代次数字。 */
  const idNow = () => screen.getByTestId('identity').textContent || ''
  const expectSubject = (expected: string) => {
    expect(idNow().startsWith(`u:${expected}@`)).toBe(true)
  }

  it('同页换号(无 storage 事件): 收到事件即刻换身份, 不等 1s 轮询', () => {
    localStorage.setItem('token', tokenOf('alice'))
    render(<Probe />)
    expectSubject('alice')

    // 只改 token、不广播: 轮询还没到点, 身份暂时还是旧的(这正是那 1s 空窗)
    act(() => {
      localStorage.setItem('token', tokenOf('bob'))
    })
    expectSubject('alice')

    // 登录流程广播统一事件 ⇒ 同一帧内就换成新身份(不需要推进任何计时器)
    act(() => notifyAuthChanged())
    expectSubject('bob')
  })

  it('事件名是对外契约: 直接 dispatch 同名事件也生效', () => {
    localStorage.setItem('token', tokenOf('alice'))
    render(<Probe />)
    act(() => {
      localStorage.setItem('token', tokenOf('bob'))
      window.dispatchEvent(new Event(AUTH_CHANGED_EVENT))
    })
    expectSubject('bob')
  })

  it('登出(清 token)事件同样立刻生效, 不会把上一个账号当当前身份', () => {
    localStorage.setItem('token', tokenOf('alice'))
    render(<Probe />)
    act(() => {
      localStorage.removeItem('token')
      window.dispatchEvent(new Event(AUTH_CHANGED_EVENT))
    })
    expect(screen.getByTestId('identity').textContent).toBe('anon')
  })

  it('scopedKey 用当前身份分桶(切号后 key 自动变, 不会命中上一账号缓存)', () => {
    localStorage.setItem('token', tokenOf('alice'))
    const alice = scopedKey(['profile'])
    expect(alice[0]).toBe('profile')
    expect(String(alice[1])).toMatch(/^u:alice@\d+$/)
    localStorage.setItem('token', tokenOf('bob'))
    const bob = scopedKey(['profile'])
    expect(String(bob[1])).toMatch(/^u:bob@\d+$/)
    expect(alice[1]).not.toBe(bob[1])
  })

  it('同名不同 sub: 分桶按 sub 而不是 username(用户名可重名/可改)', () => {
    // 两个真实账号用了同一个显示名 —— 按 username 分桶就会互相命中对方的资料
    localStorage.setItem('token', tokenWith({ username: 'shared', sub: 'user-a', role: 'member' }))
    const a = currentIdentity()
    localStorage.setItem('token', tokenWith({ username: 'shared', sub: 'user-b', role: 'member' }))
    const b = currentIdentity()
    expect(a.startsWith('u:user-a@')).toBe(true)
    expect(b.startsWith('u:user-b@')).toBe(true)
    expect(a).not.toBe(b)
  })

  it('同 sub 换发 token(撤销会话后重登/静默续期): 代次变化, 分桶不得复用', () => {
    localStorage.setItem('token', tokenWith({ username: 'alice', sub: 'user-a', v: 1 }))
    const first = currentIdentity()
    localStorage.setItem('token', tokenWith({ username: 'alice', sub: 'user-a', v: 2 }))
    const second = currentIdentity()
    expect(first.startsWith('u:user-a@')).toBe(true)
    expect(second.startsWith('u:user-a@')).toBe(true)
    // 同一个人、不同代凭证 ⇒ 必须是不同的分桶(旧 token 时代的缓存不能再命中)
    expect(first).not.toBe(second)
  })

  it('无凭证(Cookie-only / 未登录): 身份不可信 ⇒ 空串 + 共享分桶, 页面据此拒绝展示/写入', () => {
    expect(currentIdentity()).toBe('')
    expect(hasTrustedIdentity()).toBe(false)
    expect(scopedKey(['profile'])).toEqual(['profile', 'anon'])
  })

  it('token 解不出 sub/username(残缺 JWT): fail-closed, 不退化成 token 分桶', () => {
    localStorage.setItem('token', 'not-a-jwt')
    expect(currentIdentity()).toBe('')
    expect(hasTrustedIdentity()).toBe(false)
    // 关键反例: 分桶里绝不能出现 token 本身(以前会退化成 `t:<整条 token>` 写进 query key)
    expect(JSON.stringify(scopedKey(['profile']))).not.toContain('not-a-jwt')
  })
})

describe('身份收尾: 登录/登出/撤销会话/注销走同一条清缓存路径', () => {
  it('notifyAuthIdentityChanged(任一口径) 都清空 TanStack 缓存(不只是 fetchAPI 那层)', () => {
    const client = makeClient()
    registerQueryClient(client)
    client.setQueryData(['profile', 'u:alice@1'], { nickname: 'A' })
    expect(client.getQueryData(['profile', 'u:alice@1'])).toBeTruthy()

    notifyAuthIdentityChanged('revoke')
    // 撤销会话后同页重新登录: 上一代 token 的条目不能再被命中
    expect(client.getQueryData(['profile', 'u:alice@1'])).toBeUndefined()
  })
})

describe('请求取消与超时', () => {
  it('默认查询不接管取消: fetchAPI 的 GET 单次重试照旧生效(全站语义未退化)', async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError('network down'))
      .mockResolvedValue(okBody({ ok: 1 }))
    const client = makeClient()
    const { result } = renderHook(() => useApiQuery<{ ok: number }>(['plain'], '/plain'), {
      wrapper: wrapper(client),
    })
    await waitFor(() => expect(result.current.data).toEqual({ ok: 1 }))

    // 自带 signal 会关掉 fetchAPI 的重试(取消权归调用方); 默认路径必须保留重试。
    expect(fetchMock).toHaveBeenCalledTimes(2)
    const init = fetchMock.mock.calls[0][1] as RequestInit
    // 这里只做"没被我们改写"的确认: 传 timeoutMs 会覆盖 fetchAPI 的 15s 默认值
    expect((init as { timeoutMs?: number }).timeoutMs).toBeUndefined()
  })

  it('cancelOnQueryAbort 的代价是局部的: 只有这些查询放弃重试(取消权归调用方)', async () => {
    fetchMock.mockRejectedValue(new TypeError('network down'))
    const client = makeClient()
    const { result } = renderHook(
      () =>
        useApiQuery(['cancellable-retry'], '/cancellable-retry', {
          cancelOnQueryAbort: true,
          requestOptions: { timeoutMs: 60_000 },
        }),
      { wrapper: wrapper(client) },
    )
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('cancelOnQueryAbort: 传入合并 signal(可取消), 上游取消时真的 abort 掉请求', async () => {
    fetchMock.mockImplementation((_url: string, init?: RequestInit) =>
      Promise.resolve(hangingBodyResponse(init)),
    )
    const client = makeClient()
    renderHook(
      () =>
        useApiQuery(['cancellable'], '/cancellable', {
          cancelOnQueryAbort: true,
          requestOptions: { timeoutMs: 60_000 },
        }),
      { wrapper: wrapper(client) },
    )
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const init = fetchMock.mock.calls[0][1] as RequestInit
    expect(init.signal).toBeInstanceOf(AbortSignal)
    expect(init.signal?.aborted).toBe(false)

    // 保存前取消在途查询(TanStack cancelQueries 走的就是这条)
    await act(async () => {
      await client.cancelQueries({ queryKey: ['cancellable'] })
    })
    expect(init.signal?.aborted).toBe(true)
  })

  it('超时覆盖响应体: 头到了但体挂住 ⇒ 到点失败为 TIMEOUT, 不是一直 pending', async () => {
    fetchMock.mockImplementation((_url: string, init?: RequestInit) =>
      Promise.resolve(hangingBodyResponse(init)),
    )
    const client = makeClient()
    const { result } = renderHook(
      () =>
        useApiQuery(['slow-body'], '/slow-body', {
          cancelOnQueryAbort: true,
          requestOptions: { timeoutMs: 30 },
        }),
      { wrapper: wrapper(client) },
    )

    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 3000 })
    const error = result.current.error as ApiTimeoutError
    expect(error).toBeInstanceOf(ApiTimeoutError)
    expect(error.kind).toBe('TIMEOUT')
    expect(error.timeoutMs).toBe(30)
    // 计时器触发时也要 abort 掉请求, 不能留着一条僵尸连接
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true)
  })

  it('环境没有 AbortSignal.any / AbortSignal.timeout 时照样取消 + 超时(不靠浏览器版本)', async () => {
    const anyDesc = Object.getOwnPropertyDescriptor(AbortSignal, 'any')
    const timeoutDesc = Object.getOwnPropertyDescriptor(AbortSignal, 'timeout')
    try {
      // 模拟老 Safari / 内嵌 WebView
      delete (AbortSignal as unknown as Record<string, unknown>).any
      delete (AbortSignal as unknown as Record<string, unknown>).timeout
      expect(typeof (AbortSignal as unknown as { any?: unknown }).any).toBe('undefined')
      expect(typeof (AbortSignal as unknown as { timeout?: unknown }).timeout).toBe('undefined')

      fetchMock.mockImplementation((_url: string, init?: RequestInit) =>
        Promise.resolve(hangingBodyResponse(init)),
      )
      const client = makeClient()
      const { result } = renderHook(
        () =>
          useApiQuery(['legacy-env'], '/legacy-env', {
            cancelOnQueryAbort: true,
            requestOptions: { timeoutMs: 30 },
          }),
        { wrapper: wrapper(client) },
      )

      await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 3000 })
      expect(result.current.error).toBeInstanceOf(ApiTimeoutError)
      expect((result.current.error as ApiTimeoutError).kind).toBe('TIMEOUT')
    } finally {
      if (anyDesc) Object.defineProperty(AbortSignal, 'any', anyDesc)
      if (timeoutDesc) Object.defineProperty(AbortSignal, 'timeout', timeoutDesc)
    }
  })

  it('正常响应(控制组): 超时机制不会把健康请求误判为失败', async () => {
    fetchMock.mockResolvedValue(okBody({ avatar: 'data:image/png;base64,QQ==' }))
    const client = makeClient()
    const { result } = renderHook(
      () =>
        useApiQuery<{ avatar: string }>(['profile-ok'], '/profile', {
          cancelOnQueryAbort: true,
          requestOptions: { timeoutMs: 500 },
        }),
      { wrapper: wrapper(client) },
    )
    await waitFor(() => expect(result.current.data?.avatar).toBe('data:image/png;base64,QQ=='))
    expect(result.current.isError).toBe(false)
  })
})
