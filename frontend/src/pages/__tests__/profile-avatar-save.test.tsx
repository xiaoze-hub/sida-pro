// @vitest-environment jsdom
/**
 * 个人中心保存头像(2026-11-07 复审): 页头/顶栏头像必须是**服务端落库后的真值**, 并处理竞态。
 *
 * 已证实的故障面:
 *  ① 保存成功后若沿用本地草稿(data URL), 一旦后端重新编码/落盘失败, 页面会显示一张服务器上
 *     并不存在的头像;
 *  ② "头像已改"只发无值事件时, 顶栏头像要额外往返一次, 期间旧读晚到会把新头像盖回去;
 *  ③ 保存请求在途时换号, 旧账号的结果不得写进新账号的页面/缓存(否则 B 看到 A 的头像);
 *  ④ 无可信身份时个人页不得展示、不得写入(fail-closed), 也不该发起个人数据请求。
 *
 * 这里用真实组件 + 真实 fetchAPI, 只打桩 `fetch` 与"图片压缩"(canvas 在 jsdom 里不可用)。
 */
import type { ReactNode } from 'react'
import { cleanup, fireEvent, render, screen, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@sida/base-ui/components/ui/toast'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 子区块各有自己的取数/入口, 与本用例无关(且会把 fetch 断言搅乱): 直接空实现。
vi.mock('@/components/profile/SessionDevicesSection', () => ({ default: () => null }))
vi.mock('@/components/profile/NotifyChannelsSection', () => ({ default: () => null }))
vi.mock('@/components/profile/DataPrivacySection', () => ({ default: () => null }))

const DRAFT_AVATAR = 'data:image/png;base64,RFJBRlQ=' // 本地上传草稿
const fileToAvatarDataUrl = vi.hoisted(() => vi.fn())
vi.mock('@/hooks/use-avatar', async importOriginal => {
  const actual = await importOriginal<typeof import('@/hooks/use-avatar')>()
  return { ...actual, fileToAvatarDataUrl }
})

import Profile from '../Profile'

function tokenWith(claims: Record<string, unknown>): string {
  return `e30.${btoa(JSON.stringify(claims))}.sig`
}
const ALICE = tokenWith({ username: 'alice', sub: 'user-a' })
const BOB = tokenWith({ username: 'bob', sub: 'user-b' })

const SERVER_AVATAR = 'data:image/png;base64,U0VSVkVSLUE=' // 服务端处理后读回的值
const ALICE_OLD = 'data:image/png;base64,T0xELUE='
const BOB_AVATAR = 'data:image/png;base64,Qk9C'

function json(data: unknown, status = 200): Response {
  return {
    status,
    headers: { get: () => null },
    json: async () => ({ code: status === 200 ? 0 : status, data, message: '' }),
  } as unknown as Response
}

function profileOf(username: string, nickname: string, avatar: string) {
  return { username, nickname, avatar, role: 'member', created_at: '2026-01-01T00:00:00Z' }
}

let fetchMock: ReturnType<typeof vi.fn>
let putResolvers: Array<(res: Response) => void>

/** 路由式 fetch 打桩: 个人资料/统计/Key 各给确定响应, PUT 由用例决定何时/如何回。 */
function installFetch(opts: { profile: () => Response; onPut?: (init: RequestInit) => void }) {
  putResolvers = []
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    const method = (init?.method || 'GET').toUpperCase()
    const path = String(url)
    if (path.endsWith('/api/profile') && method === 'GET') {
      return Promise.resolve(opts.profile())
    }
    if (path.endsWith('/api/profile') && method === 'PUT') {
      opts.onPut?.(init || {})
      return new Promise<Response>(resolve => putResolvers.push(resolve))
    }
    if (path.includes('/profile/stats/accuracy')) return Promise.resolve(json({ agents: [], scope: 'global' }))
    if (path.includes('/profile/stats')) {
      return Promise.resolve(
        json({
          prediction: { hit_count: 0, total: 0, hit_rate: null, scope: 'global', note: '' },
          watchlist_count: 0,
          position_count: 0,
          has_shadow_profile: false,
        }),
      )
    }
    if (path.includes('/keys/my')) return Promise.resolve(json({ keys: [] }))
    return Promise.resolve(json(null))
  })
}

function renderProfile() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ToastProvider>
          <Profile />
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider> as ReactNode,
  )
}

const headerImg = () => screen.getByTestId('profile-header').querySelector('img')

async function openEditorAndPickAvatar() {
  fireEvent.click(screen.getByRole('button', { name: '编辑资料' }))
  const input = document.querySelector('input[type="file"]') as HTMLInputElement
  expect(input).toBeTruthy()
  await act(async () => {
    fireEvent.change(input, { target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] } })
  })
  await waitFor(() => expect(fileToAvatarDataUrl).toHaveBeenCalled())
}

beforeEach(() => {
  localStorage.clear()
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  fileToAvatarDataUrl.mockReset()
  fileToAvatarDataUrl.mockResolvedValue(DRAFT_AVATAR)
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('个人中心: 头像保存用服务端真值 + 竞态', () => {
  it('保存后页头头像取服务端返回的值(不是本地草稿), 并广播同一个真值', async () => {
    localStorage.setItem('token', ALICE)
    const events: unknown[] = []
    const onAvatar = (e: Event) => events.push((e as CustomEvent).detail)
    window.addEventListener('sida:avatar-changed', onAvatar)

    let profileValue = profileOf('alice', 'Alice', ALICE_OLD)
    installFetch({ profile: () => json(profileValue) })
    renderProfile()
    await waitFor(() => expect(headerImg()?.getAttribute('src')).toBe(ALICE_OLD))

    await openEditorAndPickAvatar()
    fireEvent.click(screen.getByRole('button', { name: '保存资料' }))
    await waitFor(() => expect(putResolvers.length).toBe(1))

    // 服务端把头像落盘后读回的是**另一个**串(真实后端会把 data URL 重新编码/换文件名)
    profileValue = profileOf('alice', 'Alice', SERVER_AVATAR)
    putResolvers[0](json(profileValue))

    await waitFor(() => expect(headerImg()?.getAttribute('src')).toBe(SERVER_AVATAR))
    expect(headerImg()?.getAttribute('src')).not.toBe(DRAFT_AVATAR)
    // 顶栏/别的组件收到的也是服务端真值(带值事件), 不是草稿
    await waitFor(() => expect(events).toContain(SERVER_AVATAR))
    expect(events).not.toContain(DRAFT_AVATAR)
    window.removeEventListener('sida:avatar-changed', onAvatar)
  })

  it('保存请求在途时换号: 旧账号的保存结果不落到新账号页面/缓存', async () => {
    localStorage.setItem('token', ALICE)
    let profileValue = profileOf('alice', 'Alice', ALICE_OLD)
    installFetch({ profile: () => json(profileValue) })
    renderProfile()
    await waitFor(() => expect(headerImg()?.getAttribute('src')).toBe(ALICE_OLD))

    await openEditorAndPickAvatar()
    fireEvent.click(screen.getByRole('button', { name: '保存资料' }))
    await waitFor(() => expect(putResolvers.length).toBe(1))

    // 保存还没回来就换号(B 已登录 + 广播统一事件)
    profileValue = profileOf('bob', 'Bob', BOB_AVATAR)
    act(() => {
      localStorage.setItem('token', BOB)
      window.dispatchEvent(new Event('sida:auth-changed'))
    })
    await waitFor(() => expect(headerImg()?.getAttribute('src')).toBe(BOB_AVATAR))

    // A 的保存现在才成功 —— 必须被丢弃, 页面仍是 B
    putResolvers[0](json(profileOf('alice', 'Alice', SERVER_AVATAR)))
    await act(async () => {
      await Promise.resolve()
    })
    expect(headerImg()?.getAttribute('src')).toBe(BOB_AVATAR)
    expect(screen.getByText('Bob')).toBeTruthy()
  })

  it('无法确认身份(无本地凭证): fail-closed, 不展示个人资料也不发个人数据请求', async () => {
    installFetch({ profile: () => json(profileOf('alice', 'Alice', ALICE_OLD)) })
    renderProfile()
    expect(screen.getByTestId('profile-identity-unknown')).toBeTruthy()
    expect(screen.queryByTestId('profile-header')).toBeNull()
    // 不能"先拉再藏": 请求本身就不该发出去
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
