// @vitest-environment jsdom
/**
 * 个人头像多账号隔离(2026-11-07)。
 *
 * 真实故障: 头像读写走的是 `/settings/avatar`(全局单行 `ui_avatar`), 不是个人资料 ——
 * 任何账号(含 member)看到的都是"上一个人"的头像; 且 `useAvatar` 的模块级缓存不按身份
 * 分区、不等加载完成就回退旧值, 登录页 `navigate('/')` 同页换号时旧头像会一直挂着。
 *
 * 这里钉死四条:
 *  1. 只认个人资料接口 `/api/profile`(按用户隔离), 不再读写全局设置头像;
 *  2. 未加载完成 ⇒ 空串(调用方回退首字母/图标), 绝不展示上一个账号的头像;
 *  3. 异步乱序: 旧账号迟到的响应不覆盖、不落缓存到当前账号;
 *  4. 读取失败同样只给空串, 不把"失败"当"沿用旧头像"。
 */
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))
vi.mock('@sida/api', () => ({ fetchAPI: api.fetchAPI }))

import { resetAvatarCache, saveAvatar, useAvatar } from '../use-avatar'

const AVATAR_ALICE = 'data:image/png;base64,QUxJQ0U='
const AVATAR_BOB = 'data:image/png;base64,Qk9C'
const AVATAR_ALICE_LATE = 'data:image/png;base64,QUxJQ0UtTEFURQ=='

/** 造一个能被 jwt 解析出 username 的 token(内容不参与鉴权, 只做身份判据)。 */
function tokenOf(name: string): string {
  return `e30.${btoa(JSON.stringify({ username: name, sub: name }))}.sig`
}

function setAccountToken(name: string): void {
  localStorage.setItem('token', tokenOf(name))
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** 换号: 同页(登录页 navigate 回 SPA)只改 localStorage; 别的标签页换号会额外收到 storage 事件。 */
function switchAccount(name: string, opts: { storageEvent?: boolean } = {}): void {
  setAccountToken(name)
  if (opts.storageEvent !== false) {
    window.dispatchEvent(new StorageEvent('storage', { key: 'token', newValue: tokenOf(name) }))
  }
}

beforeEach(() => {
  localStorage.clear()
  api.fetchAPI.mockReset()
  resetAvatarCache()
})

afterEach(() => {
  cleanup()
  localStorage.clear()
  resetAvatarCache()
})

describe('个人头像按账号隔离', () => {
  it('首次加载: 先空后本人头像, 且读的是个人资料接口', async () => {
    setAccountToken('alice')
    const pending = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(pending.promise)

    const { result } = renderHook(() => useAvatar())
    expect(result.current).toBe('')

    await act(async () => {
      pending.resolve({ avatar: AVATAR_ALICE })
      await pending.promise
    })
    await waitFor(() => expect(result.current).toBe(AVATAR_ALICE))

    expect(api.fetchAPI).toHaveBeenCalledTimes(1)
    expect(api.fetchAPI).toHaveBeenCalledWith('/profile', expect.objectContaining({ cacheMode: 'reload' }))
    expect(api.fetchAPI.mock.calls.some(([path]) => String(path).includes('/settings/avatar'))).toBe(false)
  })

  it('切账号: 新账号没加载完之前显示空白, 不沿用旧账号头像', async () => {
    setAccountToken('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const { result } = renderHook(() => useAvatar())
    await waitFor(() => expect(result.current).toBe(AVATAR_ALICE))

    const bobPending = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(bobPending.promise)
    act(() => switchAccount('bob'))

    // bob 的请求还没回来 —— 这里必须是空, 而不是 alice 的头像
    expect(result.current).toBe('')

    await act(async () => {
      bobPending.resolve({ avatar: AVATAR_BOB })
      await bobPending.promise
    })
    await waitFor(() => expect(result.current).toBe(AVATAR_BOB))
  })

  it('异步乱序: 旧账号迟到的响应不覆盖当前账号', async () => {
    setAccountToken('alice')
    const alicePending = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(alicePending.promise)
    const { result } = renderHook(() => useAvatar())

    // alice 还没回来就换成 bob
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_BOB })
    act(() => switchAccount('bob'))
    await waitFor(() => expect(result.current).toBe(AVATAR_BOB))

    // alice 的响应现在才到 —— 必须被丢弃
    await act(async () => {
      alicePending.resolve({ avatar: AVATAR_ALICE_LATE })
      await alicePending.promise
    })
    expect(result.current).toBe(AVATAR_BOB)

    // 新挂载的组件也不会从缓存里读到 alice
    const other = renderHook(() => useAvatar())
    await waitFor(() => expect(other.result.current).toBe(AVATAR_BOB))
  })

  it('旧账号迟到响应不写进新账号缓存: 重新挂载仍是新账号结果', async () => {
    setAccountToken('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const first = renderHook(() => useAvatar())
    await waitFor(() => expect(first.result.current).toBe(AVATAR_ALICE))

    const bobPending = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(bobPending.promise)
    act(() => switchAccount('bob'))
    act(() => {
      bobPending.resolve({ avatar: AVATAR_BOB })
    })
    await waitFor(() => expect(first.result.current).toBe(AVATAR_BOB))

    first.unmount()
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_BOB })
    const second = renderHook(() => useAvatar())
    await waitFor(() => expect(second.result.current).toBe(AVATAR_BOB))
    expect(second.result.current).not.toBe(AVATAR_ALICE)
  })

  it('同页换号(无 storage 事件): 任意一次重渲染即清掉旧账号头像并重取', async () => {
    // 登录页 `navigate('/')` 是同页换号, 不触发 storage 事件、组件也不重挂 —— 只能靠重渲染发现身份变了
    setAccountToken('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const hook = renderHook(() => useAvatar())
    await waitFor(() => expect(hook.result.current).toBe(AVATAR_ALICE))

    const bobPending = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(bobPending.promise)
    act(() => setAccountToken('bob'))
    hook.rerender()

    expect(hook.result.current).toBe('')
    await act(async () => {
      bobPending.resolve({ avatar: AVATAR_BOB })
      await bobPending.promise
    })
    await waitFor(() => expect(hook.result.current).toBe(AVATAR_BOB))
  })

  it('读取失败: 空白, 不回退到上一个账号的头像', async () => {
    setAccountToken('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const { result } = renderHook(() => useAvatar())
    await waitFor(() => expect(result.current).toBe(AVATAR_ALICE))

    api.fetchAPI.mockRejectedValueOnce(new Error('network down'))
    act(() => switchAccount('bob'))
    await waitFor(() => expect(api.fetchAPI).toHaveBeenCalledTimes(2))
    expect(result.current).toBe('')
  })

  it('未登录不发请求(避免 401 触发登出跳转), 显示空白', async () => {
    const { result } = renderHook(() => useAvatar())
    expect(result.current).toBe('')
    expect(api.fetchAPI).not.toHaveBeenCalled()
  })

  it('保存走个人资料接口(PUT /profile), 成功后跨组件同步', async () => {
    setAccountToken('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: '' })
    const { result } = renderHook(() => useAvatar())
    await waitFor(() => expect(api.fetchAPI).toHaveBeenCalledTimes(1))
    expect(result.current).toBe('')

    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    await act(async () => {
      await saveAvatar(AVATAR_ALICE)
    })

    const calls = api.fetchAPI.mock.calls
    const [path, options] = calls[calls.length - 1] as [string, RequestInit]
    expect(path).toBe('/profile')
    expect(options.method).toBe('PUT')
    expect(JSON.parse(String(options.body))).toEqual({ avatar: AVATAR_ALICE })
    await waitFor(() => expect(result.current).toBe(AVATAR_ALICE))
  })

  it('切号后迟到的保存结果不喂给新账号', async () => {
    setAccountToken('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const { result } = renderHook(() => useAvatar())
    await waitFor(() => expect(result.current).toBe(AVATAR_ALICE))

    const saving = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(saving.promise)
    const pendingSave = saveAvatar(AVATAR_ALICE_LATE)

    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_BOB })
    act(() => switchAccount('bob'))
    await waitFor(() => expect(result.current).toBe(AVATAR_BOB))

    await act(async () => {
      saving.resolve({ avatar: AVATAR_ALICE_LATE })
      await pendingSave
    })
    expect(result.current).toBe(AVATAR_BOB)
  })
})
