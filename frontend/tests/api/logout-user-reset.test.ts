// @vitest-environment jsdom
/**
 * 登出必须把**本地身份**一起清掉(2026-11-07 复审)。
 *
 * 故障面: `logout()` 只删 `token` / `token_expires`, 留下 `user` —— 而这个键是好几处私有状态的
 * 分区依据(`quote-universe` 的自选记忆、`system-status` 的快照缓存、头像、设置页的当前用户)。
 * 于是: A 登出 → B 在同一浏览器登录, 只要 B 的登录/注册响应里没带 `user`,
 * 页面继续按 **A 的 id** 读写这些状态, 等于把 A 的选择/资料挂到 B 身上。
 *
 * 对照组: 注销(DataPrivacySection)早已同时删三个键, 登出与它必须是同一套语义。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  AUTH_IDENTITY_EVENT,
  logout,
  notifyAuthIdentityChanged,
  type AuthIdentityChangeReason,
} from '@sida/api'

function tokenWith(claims: Record<string, unknown>): string {
  return `e30.${btoa(JSON.stringify(claims))}.sig`
}

const A_TOKEN = tokenWith({ username: 'alice', sub: 'user-a' })
const A_USER = { id: 'user-a', username: 'alice', role: 'member' }

const realLocation = window.location

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  notifyAuthIdentityChanged('login')
  // logout() 收尾会赋值 window.location.href(跳落地页); jsdom 真跳会报 Not implemented, 换掉。
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: { href: '', pathname: '/stocks', search: '', hash: '' },
  })
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200 })))
})

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, writable: true, value: realLocation })
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
})

describe('logout(): 清本地身份', () => {
  it('token / token_expires / user 三个键一起清掉(与注销同语义)', () => {
    localStorage.setItem('token', A_TOKEN)
    localStorage.setItem('token_expires', '2099-01-01T00:00:00Z')
    localStorage.setItem('user', JSON.stringify(A_USER))

    logout()

    expect(localStorage.getItem('token')).toBeNull()
    expect(localStorage.getItem('token_expires')).toBeNull()
    expect(localStorage.getItem('user')).toBeNull()
  })

  it('登出后身份变更事件仍然是 logout(顺序: 先清键, 再广播)', () => {
    localStorage.setItem('token', A_TOKEN)
    localStorage.setItem('user', JSON.stringify(A_USER))
    const reasons: AuthIdentityChangeReason[] = []
    const onChanged = (event: Event) => {
      reasons.push((event as CustomEvent<{ reason: AuthIdentityChangeReason }>).detail.reason)
      // 监听方在广播时读到的必须是"已经清干净"的状态
      expect(localStorage.getItem('user')).toBeNull()
    }
    window.addEventListener(AUTH_IDENTITY_EVENT, onChanged)

    logout()

    window.removeEventListener(AUTH_IDENTITY_EVENT, onChanged)
    expect(reasons).toEqual(['logout'])
  })
})
