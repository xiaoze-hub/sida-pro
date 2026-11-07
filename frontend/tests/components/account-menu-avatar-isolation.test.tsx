// @vitest-environment jsdom
/**
 * 头像菜单(真实组件 AccountMenu)在"两账号切换"下的隔离回归(2026-11-07)。
 *
 * 故障背景: 右上角头像读的是全局设置 `/settings/avatar`(唯一一行 ui_avatar),
 * 换号后仍是上一个人的头像; 且缓存不按身份分区, 旧账号头像会一直挂着。
 *
 * 本文件用真实组件 + mock 接口, 走一遍 alice → bob 的换号(含"新账号还没加载完"的
 * 中间态): 中间态只能回退到默认图标, 绝不能把 alice 的头像贴给 bob。
 */
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))
vi.mock('@sida/api', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  fetchAPI: api.fetchAPI,
}))

import AccountMenu from '@/components/AccountMenu'
import { resetAvatarCache } from '@/hooks/use-avatar'

const AVATAR_ALICE = 'data:image/png;base64,QUxJQ0U='
const AVATAR_BOB = 'data:image/png;base64,Qk9C'

function tokenOf(name: string): string {
  return `e30.${btoa(JSON.stringify({ username: name, sub: name }))}.sig`
}

function useAccount(name: string): void {
  localStorage.setItem('token', tokenOf(name))
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

function mount() {
  return render(
    <MemoryRouter>
      <AccountMenu
        navItems={[]}
        mode="light"
        onSetMode={() => {}}
        onOpenSelfCheck={() => {}}
      />
    </MemoryRouter>,
  )
}

/** 头像 <img> 只在"确实有头像"时渲染; 无头像回退到默认 User 图标。 */
function avatarImg(container: HTMLElement): HTMLElement | null {
  return container.querySelector('img[alt="头像"]')
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

describe('头像菜单两账号切换', () => {
  it('显示当前账号头像; 换号加载中回退默认图标(不贴旧账号头像), 加载后换成新账号头像', async () => {
    useAccount('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const view = mount()

    await waitFor(() => expect(avatarImg(view.container)?.getAttribute('src')).toBe(AVATAR_ALICE))
    expect(api.fetchAPI.mock.calls[0][0]).toBe('/profile')
    expect(api.fetchAPI.mock.calls.some(([path]) => String(path).includes('/settings/avatar'))).toBe(false)

    // 换到 bob, 请求挂起: 中间态必须是默认图标, 不能还是 alice 的图
    const bobPending = deferred<{ avatar?: string }>()
    api.fetchAPI.mockReturnValueOnce(bobPending.promise)
    act(() => {
      useAccount('bob')
      window.dispatchEvent(new StorageEvent('storage', { key: 'token', newValue: tokenOf('bob') }))
    })
    expect(avatarImg(view.container)).toBeNull()

    act(() => {
      bobPending.resolve({ avatar: AVATAR_BOB })
    })
    await waitFor(() => expect(avatarImg(view.container)?.getAttribute('src')).toBe(AVATAR_BOB))
  })

  it('两个账号先后挂载: 各自只拿到自己的头像', async () => {
    useAccount('alice')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_ALICE })
    const alice = mount()
    await waitFor(() => expect(avatarImg(alice.container)?.getAttribute('src')).toBe(AVATAR_ALICE))
    alice.unmount()

    useAccount('bob')
    api.fetchAPI.mockResolvedValueOnce({ avatar: AVATAR_BOB })
    const bob = mount()
    await waitFor(() => expect(avatarImg(bob.container)?.getAttribute('src')).toBe(AVATAR_BOB))
    expect(avatarImg(bob.container)?.getAttribute('src')).not.toBe(AVATAR_ALICE)
  })
})
