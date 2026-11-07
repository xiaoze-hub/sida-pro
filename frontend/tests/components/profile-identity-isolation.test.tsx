// @vitest-environment jsdom
//
// 个人中心账号隔离(2026-11-07)。
//
// 真实故障面: 登录页换号走 `navigate('/')`(同标签页 SPA, 不刷新), TanStack 缓存里
// `['profile']` / `['profile','stats']` 这些 key **不含身份** ⇒ B 登录后个人中心先在
// A 的资料上渲染(昵称/头像/统计/未保存的编辑草稿全串味), 直到 B 的数据回来才纠正。
//
// 本文件用**真实 Profile 组件** + 真实 useApiQuery/TanStack, 只 mock 网络层, 钉死:
//  1. 同标签页换号(无 storage 事件)后立刻不再显示上一个账号的资料, 且按新身份重取;
//  2. 上一个账号迟到的响应落不到当前账号(每个身份各自的 key 分桶);
//  3. 未保存的昵称/头像草稿不跨账号(换人即关编辑框、清草稿);
//  4. 保存期间在途的旧读晚到, 不覆盖刚保存的新头像。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void }

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(res => {
    resolve = res
  })
  return { promise, resolve }
}

const AVATAR_NEW = 'data:image/png;base64,TkVX' // 保存后的新头像
const AVATAR_OLD = '' // 保存前的旧值(无头像)

interface Account {
  nickname: string
  avatar: string
  watchlist: number
}

const store = {
  accounts: {} as Record<string, Account>,
  /** `GET /profile#<identity>` 之类被扣住的读响应 */
  held: {} as Record<string, Deferred<unknown> | undefined>,
  calls: [] as Array<{ path: string; identity: string }>,
  puts: [] as Array<{ body: Record<string, unknown>; identity: string }>,
}

function tokenOf(name: string): string {
  return `e30.${btoa(JSON.stringify({ username: name, sub: name }))}.sig`
}

function identityOf(name: string): string {
  return `u:${name}`
}

/** 网络层看到的身份(后端就是这么认人的: Authorization/Cookie 对应的登录身份)。 */
function requestIdentity(): string {
  const token = localStorage.getItem('token') || ''
  try {
    const payload = JSON.parse(atob(token.split('.')[1] || ''))
    return typeof payload.username === 'string' ? identityOf(payload.username) : ''
  } catch {
    return ''
  }
}

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()
  return { ...actual, fetchAPI: api.fetchAPI }
})

import { Profile } from '@/pages/Profile'
import { AUTH_CHANGED_EVENT } from '@/hooks/useApiQuery'

function mount(section = 'account') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const utils = render(
    <MemoryRouter initialEntries={[`/profile?section=${section}`]}>
      <QueryClientProvider client={client}>
        <Profile />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return { ...utils, client }
}

function statsPayload(watchlist: number) {
  return {
    prediction: { hit_count: 0, total: 0, hit_rate: null, scope: 'global', note: '' },
    watchlist_count: watchlist,
    position_count: 0,
    has_shadow_profile: false,
  }
}

/** 按真实后端语义返回: 响应内容取决于**请求时的登录身份**。 */
function installFakeApi() {
  api.fetchAPI.mockImplementation((path: string, options?: { method?: string; body?: string }) => {
    const identity = requestIdentity()
    store.calls.push({ path, identity })
    const account = store.accounts[identity] ?? { nickname: '', avatar: '', watchlist: 0 }

    if (path === '/profile') {
      if (options?.method === 'PUT') {
        const body = JSON.parse(String(options.body || '{}')) as Record<string, unknown>
        store.puts.push({ body, identity })
        // 服务端落库后回显: 昵称以提交为准, 头像以提交为准
        return Promise.resolve({
          username: identity.replace(/^u:/, ''),
          nickname: body.nickname,
          avatar: typeof body.avatar === 'string' ? body.avatar : account.avatar,
          role: 'member',
          created_at: null,
        })
      }
      const held = store.held[`GET /profile#${identity}`]
      if (held) return held.promise
      return Promise.resolve({
        username: identity.replace(/^u:/, ''),
        nickname: account.nickname,
        avatar: account.avatar,
        role: 'member',
        created_at: null,
      })
    }
    if (path === '/profile/stats') {
      const held = store.held[`GET /profile/stats#${identity}`]
      if (held) return held.promise
      return Promise.resolve(statsPayload(account.watchlist))
    }
    if (path === '/profile/stats/accuracy') {
      return Promise.resolve({ since: null, scope: 'global', note: '', overall: {}, agents: [] })
    }
    if (path === '/auth/sessions') return Promise.resolve({ sessions: [], current_id: null })
    if (path === '/channels') return Promise.resolve([])
    if (path === '/keys/my') return Promise.resolve({ keys: [] })
    if (path === '/user/data/delete/status') return Promise.resolve({ status: 'none' })
    return Promise.resolve({})
  })
}

function profileGetCount(): number {
  return store.calls.filter(c => c.path === '/profile').length
}

beforeEach(() => {
  localStorage.clear()
  store.accounts = {}
  store.held = {}
  store.calls = []
  store.puts = []
  api.fetchAPI.mockReset()
  installFakeApi()
  store.accounts[identityOf('alice')] = { nickname: '老板A', avatar: '', watchlist: 111 }
  store.accounts[identityOf('bob')] = { nickname: '老板B', avatar: '', watchlist: 222 }
  localStorage.setItem('token', tokenOf('alice'))
})

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.useRealTimers()
})

describe('个人中心按登录身份隔离 (2026-11-07)', () => {
  it('同标签页换号(无 storage 事件): 不再显示上一个账号的资料, 并按新身份重取', async () => {
    mount()
    expect(await screen.findByText('老板A')).toBeTruthy()
    expect(profileGetCount()).toBe(1)

    // 登录页换号只写 localStorage + navigate('/'): 组件不重挂, 也没有 storage 事件
    act(() => {
      localStorage.setItem('token', tokenOf('bob'))
    })

    // 身份变了 ⇒ 旧账号的昵称/统计立刻下屏(不能等 B 的数据回来才纠正)
    await waitFor(() => expect(screen.queryByText('老板A')).toBeNull(), { timeout: 6000 })
    expect(await screen.findByText('老板B')).toBeTruthy()

    // 关键: 个人资料的 key 带身份 ⇒ 即使还在 30s staleTime 内也会为 B 重取,
    // 而不是拿 A 的缓存直接渲染。
    const identities = store.calls.filter(c => c.path === '/profile').map(c => c.identity)
    expect(identities).toEqual([identityOf('alice'), identityOf('bob')])
  })

  it('上一个账号迟到的响应落不进当前账号(各自 key 分桶)', async () => {
    // 扣住 alice 的"我的数据"响应, 换号后再让它迟到落地
    const heldStats = deferred<unknown>()
    store.held[`GET /profile/stats#${identityOf('alice')}`] = heldStats
    mount('activity')
    expect(await screen.findByText('老板A')).toBeTruthy()

    act(() => {
      localStorage.setItem('token', tokenOf('bob'))
    })
    expect(await screen.findByText('老板B')).toBeTruthy()
    // bob 自己的统计(222)已上屏
    expect(await screen.findByText('222')).toBeTruthy()

    await act(async () => {
      heldStats.resolve(statsPayload(999))
      await heldStats.promise
    })

    expect(screen.queryByText('999')).toBeNull() // A 的迟到统计没有串进来
    expect(screen.getByText('222')).toBeTruthy()
  })

  it('未保存的编辑草稿不跨账号: 换人即关编辑框并清空, 不会把 A 的草稿写成 B 的资料', async () => {
    mount()
    expect(await screen.findByText('老板A')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '编辑资料' }))
    const input = (await screen.findByLabelText(/昵称/)) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'A 的未保存草稿' } })
    expect(input.value).toBe('A 的未保存草稿')

    act(() => {
      localStorage.setItem('token', tokenOf('bob'))
    })
    await waitFor(() => expect(screen.queryByDisplayValue('A 的未保存草稿')).toBeNull(), { timeout: 6000 })
    expect(await screen.findByText('老板B')).toBeTruthy()

    // 重新打开编辑框: 是 B 的资料, 不是 A 的草稿
    fireEvent.click(screen.getByRole('button', { name: '编辑资料' }))
    const reopened = (await screen.findByLabelText(/昵称/)) as HTMLInputElement
    await waitFor(() => expect(reopened.value).toBe('老板B'))
    expect(store.puts).toHaveLength(0) // 换号过程没有偷偷提交任何草稿
  })

  it('保存期间在途的旧读晚到, 不覆盖刚保存的新头像', async () => {
    // 该账号当前已有头像; 保存前发出的那次读拿到的是"还没有头像"的旧快照
    store.accounts[identityOf('alice')] = { nickname: '老板A', avatar: AVATAR_NEW, watchlist: 111 }
    const { client } = mount()
    expect(await screen.findByText('老板A')).toBeTruthy()
    expect((document.querySelector('img[alt="头像"]') as HTMLImageElement).getAttribute('src')).toBe(AVATAR_NEW)

    // 保存前先发一次读(模拟回到前台触发的重取), 把响应扣住
    const scoped = ['profile', identityOf('alice')]
    const before = profileGetCount()
    const staleRead = deferred<unknown>()
    const heldKey = `GET /profile#${identityOf('alice')}`
    store.held[heldKey] = staleRead
    await act(async () => {
      void client.invalidateQueries({ queryKey: ['profile'] })
    })
    await waitFor(() => expect(profileGetCount()).toBe(before + 1))

    // 用户改昵称并保存
    fireEvent.click(screen.getByRole('button', { name: '编辑资料' }))
    const input = (await screen.findByLabelText(/昵称/)) as HTMLInputElement
    fireEvent.change(input, { target: { value: '老板A 改名' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '保存资料' }))
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    const put = store.puts[0]
    expect(put?.identity).toBe(identityOf('alice'))
    const saved = client.getQueryData(scoped) as { nickname?: string; avatar?: string } | undefined
    expect(saved?.nickname).toBe('老板A 改名')
    expect(saved?.avatar).toBe(AVATAR_NEW)
    expect((document.querySelector('img[alt="头像"]') as HTMLImageElement).getAttribute('src')).toBe(AVATAR_NEW)

    // 旧读现在才回来, 带的是保存前的空头像 —— 必须被丢弃, 不许盖掉刚保存的结果
    delete store.held[heldKey]
    await act(async () => {
      staleRead.resolve({
        username: 'alice',
        nickname: '老板A',
        avatar: AVATAR_OLD,
        role: 'member',
        created_at: null,
      })
      await staleRead.promise
    })
    const after = client.getQueryData(scoped) as { nickname?: string; avatar?: string } | undefined
    expect(after?.avatar).toBe(AVATAR_NEW)
    expect(after?.nickname).toBe('老板A 改名')
    expect((document.querySelector('img[alt="头像"]') as HTMLImageElement).getAttribute('src')).toBe(AVATAR_NEW)
  })

  /**
   * 换号后页面还挂在旧身份上的那一小段窗口(登录已把 token 换成 B, 渲染还是 A):
   * 这是最危险的一秒 —— 用户看到的是 A 的资料和 A 的草稿, 点保存却会用 B 的凭证提交。
   * 提交前必须拿 currentIdentity() 和渲染身份对一次, 不一致就拒绝写入。
   */
  it('页面还挂在旧身份时点保存: 拒绝写入(不发 PUT), 不把 A 的草稿写成 B 的资料', async () => {
    mount()
    expect(await screen.findByText('老板A')).toBeTruthy()

    // A 的编辑框已经打开、草稿已经改好(这份草稿只属于 alice)
    fireEvent.click(screen.getByRole('button', { name: '编辑资料' }))
    const input = (await screen.findByLabelText(/昵称/)) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'A 的草稿' } })

    // 点保存前一刻 token 已被换成 bob(静默换号, 页面还没来得及重渲染成 bob)
    act(() => {
      localStorage.setItem('token', tokenOf('bob'))
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '保存资料' }))
    })

    // 一个字节都不许写到 bob 名下
    expect(store.puts).toHaveLength(0)
    expect(store.puts.filter(p => p.identity === identityOf('bob'))).toHaveLength(0)
  })

  it('收到 sida:auth-changed 即刻换身份(不等 1s 轮询), 紧接着保存写到新账号', async () => {
    mount()
    expect(await screen.findByText('老板A')).toBeTruthy()

    act(() => {
      localStorage.setItem('token', tokenOf('bob'))
      window.dispatchEvent(new Event(AUTH_CHANGED_EVENT))
    })
    // 同一帧内旧账号资料就下屏了(没有推进任何计时器 ⇒ 不是轮询兜的底)
    expect(screen.queryByText('老板A')).toBeNull()

    expect(await screen.findByText('老板B')).toBeTruthy()

    // 换号后立刻操作: 打开编辑框、改昵称、保存 —— 必须写到 bob 名下
    fireEvent.click(screen.getByRole('button', { name: '编辑资料' }))
    const input = (await screen.findByLabelText(/昵称/)) as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('老板B'))
    fireEvent.change(input, { target: { value: '老板B 改名' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '保存资料' }))
    })
    await waitFor(() => expect(store.puts).toHaveLength(1))

    expect(store.puts[0].identity).toBe(identityOf('bob'))
    expect(store.puts[0].body.nickname).toBe('老板B 改名')
  })
})
