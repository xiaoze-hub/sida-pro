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
// 只替换传输层: 身份解析(`getAuthScope`)必须用真实实现 —— 这里要验的正是"按真实身份分桶"。
vi.mock('@sida/api', async importOriginal => {
  const actual = await importOriginal<typeof import('@sida/api')>()
  return { ...actual, fetchAPI: api.fetchAPI }
})

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

  it('读取失败后重新挂载可重试, 且请求带显式超时(不靠浏览器默认)', async () => {
    setAccountToken('alice')
    api.fetchAPI.mockRejectedValueOnce(new Error('请求超时，请稍后重试'))
    const first = renderHook(() => useAvatar())
    await waitFor(() => expect(api.fetchAPI).toHaveBeenCalledTimes(1))
    expect(first.result.current).toBe('')

    // 卡住的请求会一直占着 inflight：必须给显式超时，失败后挂载才能重来
    const opts = api.fetchAPI.mock.calls[0][1] as { timeoutMs?: number }
    expect(opts.timeoutMs).toBeGreaterThan(0)

    // 页面重新挂载(= 用户重新进来)才允许重试，且不需要刷新浏览器
    first.unmount()
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const second = renderHook(() => useAvatar())
    await waitFor(() => expect(second.result.current).toBe(AVATAR_ALICE))
    expect(api.fetchAPI).toHaveBeenCalledTimes(2)
  })

  it('同账号: 早发的读晚到, 不覆盖刚保存的新头像', async () => {
    setAccountToken('alice')
    const staleRead = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(staleRead.promise)
    const { result } = renderHook(() => useAvatar())
    expect(api.fetchAPI).toHaveBeenCalledTimes(1)

    // 保存(服务端已确认新头像)
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    await act(async () => {
      await saveAvatar(AVATAR_ALICE)
    })
    await waitFor(() => expect(result.current).toBe(AVATAR_ALICE))

    // 保存前发出的读此刻才回来, 带的是旧头像 —— 必须丢弃
    await act(async () => {
      staleRead.resolve({ avatar: AVATAR_BOB })
      await staleRead.promise
    })
    expect(result.current).toBe(AVATAR_ALICE)
  })

  it('同账号并发保存: 先发后到的旧保存不覆盖新头像', async () => {
    setAccountToken('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: '' })
    const { result } = renderHook(() => useAvatar())
    await waitFor(() => expect(api.fetchAPI).toHaveBeenCalledTimes(1))

    const slowSave = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(slowSave.promise)
    const firstSave = saveAvatar('data:image/png;base64,T0xE') // 旧头像, 慢
    api.fetchAPI.mockResolvedValueOnce({ avatar: 'data:image/png;base64,TkVX' })
    await act(async () => {
      await saveAvatar('data:image/png;base64,TkVX') // 新头像, 快
    })
    await waitFor(() => expect(result.current).toBe('data:image/png;base64,TkVX'))

    await act(async () => {
      slowSave.resolve({ avatar: 'data:image/png;base64,T0xE' })
      await firstSave
    })
    expect(result.current).toBe('data:image/png;base64,TkVX')
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

  it('无值"头像已改"事件(个人中心保存后): 作废并取消在途旧读, 立即重取, 旧读不覆盖新头像', async () => {
    setAccountToken('alice')
    const staleRead = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(staleRead.promise)
    const { result } = renderHook(() => useAvatar())
    expect(api.fetchAPI).toHaveBeenCalledTimes(1)
    const staleSignal = (api.fetchAPI.mock.calls[0][1] as { signal?: AbortSignal }).signal
    expect(staleSignal?.aborted).toBe(false)

    // 个人中心保存成功 → 广播"头像已变"但不带值(新值由后端持有)
    const fresh = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(fresh.promise)
    act(() => {
      window.dispatchEvent(new CustomEvent('sida:avatar-changed'))
    })

    // 事件前发出的读已经无意义: 必须被取消, 并且真的重新发一次读
    expect(staleSignal?.aborted).toBe(true)
    expect(api.fetchAPI).toHaveBeenCalledTimes(2)

    // 旧读(保存前发出)现在才回来, 带的是保存前的值 —— 不能盖掉新头像
    await act(async () => {
      staleRead.resolve({ avatar: AVATAR_BOB })
      await staleRead.promise
    })
    expect(result.current).toBe('')
    expect(api.fetchAPI).toHaveBeenCalledTimes(2) // 过期读回来不再额外发请求

    await act(async () => {
      fresh.resolve({ avatar: AVATAR_ALICE })
      await fresh.promise
    })
    await waitFor(() => expect(result.current).toBe(AVATAR_ALICE))
  })

  it('保存途中发起的读(忽略取消)晚到, 不覆盖保存落地的新头像', async () => {
    setAccountToken('alice')
    const firstRead = deferred<{ avatar?: string }>() // 一直不回, 让缓存处于"未加载"
    api.fetchAPI.mockReturnValueOnce(firstRead.promise)
    const { result } = renderHook(() => useAvatar())
    expect(result.current).toBe('')

    const put = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(put.promise)
    const pendingSave = saveAvatar(AVATAR_ALICE)

    // 保存途中另一个组件挂载 ⇒ 发起一次读(拿到的会是保存前的值)
    const midRead = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(midRead.promise)
    const other = renderHook(() => useAvatar())
    expect(api.fetchAPI).toHaveBeenCalledTimes(3)

    await act(async () => {
      put.resolve({ avatar: AVATAR_ALICE })
      await pendingSave
    })
    await waitFor(() => expect(result.current).toBe(AVATAR_ALICE))

    // 这次读比 PUT 早提交, 现在才回来: 不能把新头像盖回旧值
    await act(async () => {
      midRead.resolve({ avatar: AVATAR_BOB })
      await midRead.promise
    })
    expect(result.current).toBe(AVATAR_ALICE)
    expect(other.result.current).toBe(AVATAR_ALICE)
  })

  it('同页切号广播 sida:auth-changed: 不等 1s 轮询, 立刻清掉旧账号头像并重取', async () => {
    setAccountToken('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const { result } = renderHook(() => useAvatar())
    await waitFor(() => expect(result.current).toBe(AVATAR_ALICE))

    const bobPending = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(bobPending.promise)
    // 登录页换号: 只写 localStorage, 但按统一约定广播事件(同页不会有 storage 事件)
    act(() => {
      setAccountToken('bob')
      window.dispatchEvent(new CustomEvent('sida:auth-changed'))
    })

    // 同一帧内就必须是空白 —— 不能等到下一次 1s 轮询/某次无关重渲染
    expect(result.current).toBe('')
    expect(api.fetchAPI).toHaveBeenCalledTimes(2)

    await act(async () => {
      bobPending.resolve({ avatar: AVATAR_BOB })
      await bobPending.promise
    })
    await waitFor(() => expect(result.current).toBe(AVATAR_BOB))
  })

  it('多个组件同时挂载: 同一次无值事件只重取一次(不按监听器数量放大请求)', async () => {
    setAccountToken('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const first = renderHook(() => useAvatar())
    await waitFor(() => expect(first.result.current).toBe(AVATAR_ALICE))
    const second = renderHook(() => useAvatar()) // 读的是同一份模块缓存, 不再发请求
    expect(api.fetchAPI).toHaveBeenCalledTimes(1)

    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_BOB })
    act(() => {
      window.dispatchEvent(new CustomEvent('sida:avatar-changed'))
    })
    expect(api.fetchAPI).toHaveBeenCalledTimes(2)

    await waitFor(() => expect(first.result.current).toBe(AVATAR_BOB))
    expect(second.result.current).toBe(AVATAR_BOB)
  })
})
