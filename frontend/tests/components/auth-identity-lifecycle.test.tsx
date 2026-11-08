// @vitest-environment jsdom
/**
 * 跨标签页换号的身份生命周期钉子 —— 2026-11-08。
 *
 * 已证实的故障面(真 keep-alive 的代价):
 *   ① 页签面板**常驻挂载**(隐藏不卸), 页面私有 `useState`(草稿、筛选、展开态、上一账号的
 *      个人资料)在换号后**不会自动清空** —— 下一个用户在同一台电脑上打开这个标签页,
 *      看到的是上一个用户的页面残影;
 *   ② 另一个标签页登录/登出只改 localStorage, 本标签页的 `RequireAuth` 只在**路由变化**时
 *      复检登录态 ⇒ 全程没有任何一帧被拦下;
 *   ③ 换号瞬间若继续渲染旧页面, 新 token 的轮询/在途响应会落进旧实例(或反过来, 旧世界的
 *      迟到响应写进新用户的界面), 两侧都是串号。
 *
 * 判据(每条都是一个 it):
 *   · 换人: 先**封锁**(旧页面与旧 useState 一个像素都不显示), 再以新账号**重建**实例;
 *   · 迟到: 旧世界组件的 setState 在卸载后不得出现在新用户的 DOM 里;
 *   · 同人 token 轮换: 不重挂、不丢页签内状态(那是本人的状态, 不是别人的);
 *   · 登出 / 凭证不可解析: fail-closed(封锁 + 转登录), 绝不回退到旧用户页面;
 *   · 普通渲染与站内导航、首次登录落地路径不受影响。
 */
import { useEffect, useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { getAuthIdentity, getAuthScope } from '@sida/api'
import RequireAuth from '@/components/auth/RequireAuth'
import { useAuthIdentityGate } from '@/hooks/useAuthIdentityGate'

function token(claims: Record<string, unknown>): string {
  return `e30.${btoa(JSON.stringify(claims))}.sig`
}

const A_TOKEN = token({ sub: 'user-a', username: 'alice', jti: 'a1' })
const A_TOKEN_2 = token({ sub: 'user-a', username: 'alice', jti: 'a2' }) // 同一人, 新发 token
const B_TOKEN = token({ sub: 'user-b', username: 'bob', jti: 'b1' }) // 换了人

function signIn(credential: string) {
  localStorage.setItem('token', credential)
  localStorage.setItem('token_expires', new Date(Date.now() + 3_600_000).toISOString())
  localStorage.setItem('user', JSON.stringify({ username: 'stale' }))
}

function signOut() {
  localStorage.removeItem('token')
  localStorage.removeItem('token_expires')
  localStorage.removeItem('user')
}

/** 模拟**另一个标签页**改了凭证: 先落存储, 再派发 storage 事件(浏览器真实顺序)。 */
function crossTab(next: string | null) {
  if (next) signIn(next)
  else signOut()
  act(() => {
    window.dispatchEvent(new StorageEvent('storage', { key: 'token', newValue: next }))
  })
}

/** 旧世界留下的 setState —— 故意不解除, 模拟"页面没有自己防护"的真实情况。 */
let lateSetters: Array<(value: string) => void> = []
let mountCount = 0

function PrivatePage() {
  // 挂载时所在的"身份世界"; 只有真的重建实例才会变成新账号
  const [bornAs] = useState(() => getAuthIdentity() ?? 'anon')
  const [draft, setDraft] = useState('')
  const [remote, setRemote] = useState('')
  useEffect(() => {
    mountCount += 1
    lateSetters.push(setRemote)
  }, [])
  return (
    <div data-testid="private-page" data-born-as={bornAs}>
      <span data-testid="born-as">{bornAs}</span>
      <input data-testid="draft" value={draft} onChange={(e) => setDraft(e.target.value)} />
      <span data-testid="remote">{remote}</span>
    </div>
  )
}

function Gate({ children, landing }: { children: React.ReactNode; landing?: React.ReactNode }) {
  const identity = useAuthIdentityGate()
  return (
    <RequireAuth identity={identity} landing={landing}>
      {children}
    </RequireAuth>
  )
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}</div>
}

function NavButton({ to }: { to: string }) {
  const navigate = useNavigate()
  return (
    <button type="button" data-testid={`go-${to}`} onClick={() => navigate(to)}>
      go {to}
    </button>
  )
}

function renderGuarded(path = '/heatmap') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <NavButton to="/" />
      <Gate landing={<div data-testid="landing">落地页</div>}>
        <Routes>
          <Route path="/" element={<PrivatePage />} />
          <Route path="/heatmap" element={<PrivatePage />} />
          <Route path="/ai-copilot" element={<div data-testid="copilot-page">参谋</div>} />
        </Routes>
      </Gate>
    </MemoryRouter>,
  )
}

const reloadingPanel = () => screen.queryByTestId('auth-identity-reloading')
const privatePage = () => screen.queryByTestId('private-page')
const locationNow = () => screen.getByTestId('location').textContent ?? ''
const draftValue = () =>
  (screen.getByTestId('draft') as HTMLInputElement).value

beforeEach(() => {
  localStorage.clear()
  lateSetters = []
  mountCount = 0
  // 首次登录落地跳转(RequireAuth 保留的既有行为): 测试里默认视为老用户, 避免干扰
  localStorage.setItem('sida_first_run_landed', '1')
})
afterEach(() => {
  cleanup()
  localStorage.clear()
})

describe('跨标签页换人: 旧用户的页面必须立刻消失, 新用户拿到全新实例', () => {
  it('换人事件到达先封锁, 再以新账号重建(旧 useState 不残留)', async () => {
    signIn(A_TOKEN)
    renderGuarded('/heatmap')
    expect(screen.getByTestId('born-as').textContent).toBe('u:user-a')
    fireEvent.change(screen.getByTestId('draft'), { target: { value: 'A-草稿' } })
    expect(draftValue()).toBe('A-草稿')
    expect(mountCount).toBe(1)

    crossTab(B_TOKEN)

    // ① 同步的这一帧: 旧页面与旧草稿一个都不许留在 DOM 里
    expect(privatePage()).toBeNull()
    expect(screen.queryByDisplayValue('A-草稿')).toBeNull()
    expect(reloadingPanel()).not.toBeNull()

    // ② 确认之后: 是**新实例**(not 被复用的旧实例)
    await waitFor(() => screen.getByTestId('born-as'))
    expect(screen.getByTestId('born-as').textContent).toBe('u:user-b')
    expect(draftValue()).toBe('')
    expect(mountCount).toBe(2)
  })

  it('旧世界的迟到响应不得写进新用户的页面', async () => {
    signIn(A_TOKEN)
    renderGuarded('/heatmap')
    const staleSetter = lateSetters[0]
    expect(staleSetter).toBeTypeOf('function')

    crossTab(B_TOKEN)
    await waitFor(() => expect(screen.getByTestId('born-as').textContent).toBe('u:user-b'))

    act(() => staleSetter('A-私有数据'))
    expect(screen.queryByText('A-私有数据')).toBeNull()
    expect(screen.getByTestId('remote').textContent).toBe('')
  })

  it('同名不同 sub(两个 alice)仍按"换人"处理: 不许按 username 合并', async () => {
    signIn(A_TOKEN)
    renderGuarded('/heatmap')
    fireEvent.change(screen.getByTestId('draft'), { target: { value: 'A-草稿' } })

    // username 同为 alice, 但 sub 不同 ⇒ 是两个人(身份判据以 sub 优先, 见 @sida/api)
    crossTab(token({ sub: 'user-c', username: 'alice', jti: 'c1' }))

    expect(privatePage()).toBeNull()
    await waitFor(() => expect(screen.getByTestId('born-as').textContent).toBe('u:user-c'))
    expect(draftValue()).toBe('')
  })

  it('跨标签页登出: 旧页面消失并回到登录页', async () => {
    signIn(A_TOKEN)
    renderGuarded('/heatmap')
    expect(privatePage()).not.toBeNull()

    crossTab(null)

    expect(privatePage()).toBeNull()
    expect(reloadingPanel()).not.toBeNull()
    await waitFor(() => expect(locationNow()).toBe('/login'))
    expect(privatePage()).toBeNull()
  })

  it('在首页(/)被登出: 不显示旧用户的首页, 落到公开落地页', async () => {
    signIn(A_TOKEN)
    renderGuarded('/')
    expect(privatePage()).not.toBeNull()

    crossTab(null)
    await waitFor(() => screen.getByTestId('landing'))
    expect(privatePage()).toBeNull()
  })

  it('凭证无法解析(坏 token): fail-closed, 绝不回退到旧用户页面', async () => {
    signIn(A_TOKEN)
    renderGuarded('/heatmap')

    crossTab('garbage-not-a-jwt')

    await waitFor(() => expect(locationNow()).toBe('/login'))
    expect(privatePage()).toBeNull()
    expect(reloadingPanel()).toBeNull() // 已裁决完: 不是一直卡在过渡块
  })
})

describe('同一用户 token 轮换: 是本人的状态, 不重挂也不丢页签内状态', () => {
  it('同 sub 新 token(撤销会话后重登 / 静默续期) → 不封锁、不重建、草稿还在', async () => {
    signIn(A_TOKEN)
    const scopeBefore = getAuthScope()
    renderGuarded('/heatmap')
    fireEvent.change(screen.getByTestId('draft'), { target: { value: '本人草稿' } })
    expect(mountCount).toBe(1)

    crossTab(A_TOKEN_2)

    // 不进入"重新确认"过渡态(同一个人, 没有要藏的前用户页面)
    expect(reloadingPanel()).toBeNull()
    expect(screen.getByTestId('born-as').textContent).toBe('u:user-a')
    expect(draftValue()).toBe('本人草稿')
    expect(mountCount).toBe(1)
    // 但凭证代次必须已经推进(旧世界响应/缓存不得被当成本次会话)
    await waitFor(() => expect(getAuthScope()).not.toBe(scopeBefore))
  })
})

describe('不能破坏原有登录与普通导航', () => {
  it('已登录正常渲染, 站内导航不触发身份过渡块', async () => {
    signIn(A_TOKEN)
    renderGuarded('/heatmap')
    expect(privatePage()).not.toBeNull()
    expect(reloadingPanel()).toBeNull()

    fireEvent.click(screen.getByTestId('go-/'))
    await waitFor(() => expect(locationNow()).toBe('/'))
    expect(privatePage()).not.toBeNull()
    expect(reloadingPanel()).toBeNull()
  })

  it('未登录访问功能页 → 转 /login; 访问 / → 落地页', async () => {
    const first = renderGuarded('/heatmap')
    await waitFor(() => expect(locationNow()).toBe('/login'))
    expect(privatePage()).toBeNull()
    first.unmount()

    renderGuarded('/')
    await waitFor(() => screen.getByTestId('landing'))
    expect(privatePage()).toBeNull()
  })

  it('首次登录落地仍落 /ai-copilot(既有行为不改)', async () => {
    signIn(A_TOKEN)
    localStorage.removeItem('sida_first_run_landed')
    renderGuarded('/')
    await waitFor(() => expect(locationNow()).toBe('/ai-copilot'))
    expect(screen.getByTestId('copilot-page')).not.toBeNull()
  })
})
