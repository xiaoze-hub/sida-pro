// @vitest-environment jsdom
/**
 * 登录/注册后的**本地身份**必须换代或被清掉(2026-11-07 复审) —— 真实 Login 页面, 只打桩 `fetch`。
 *
 * 故障面: `saveToken()` 只在响应**带 user** 时写 `user`, 不带就原样留着上一个人的 `user`。
 *   - 密码登录 / 验证码登录的响应体里 `user` 是可选的;
 *   - 注册响应只保证 `token` / `expires_at`(甚至明确可以为无 user)。
 * 于是 A 登出(或直接在登录页换号)后 B 登录, `localStorage.user` 还是 A 的 id ——
 * 自选记忆、系统状态快照、头像、设置页的"当前用户"全按 A 的身份读写。
 *
 * 反向要求(不能修过头): 响应带 user 时必须写入**当前这个人**, 不能因为清理把新身份也丢掉。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@sida/base-ui/components/ui/toast'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import LoginPage from '@/pages/Login'

function tokenWith(claims: Record<string, unknown>): string {
  return `e30.${btoa(JSON.stringify(claims))}.sig`
}

const A_USER = { id: 'user-a', username: 'alice', role: 'member' }
const B_TOKEN = tokenWith({ username: 'bob', sub: 'user-b' })
const B_USER = { id: 'user-b', username: 'bob', role: 'member' }
const EXPIRES = '2099-01-01T00:00:00Z'

type Handler = () => unknown

let routes: Record<string, Handler>

function apiOk(data: unknown) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => ({ code: 0, success: true, data, message: '' }),
  }
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  routes = {
    'GET /api/auth/status': () => ({
      initialized: true,
      multi_user: true,
      register_mode: 'open',
      email_configured: true,
    }),
    'GET /api/auth/captcha': () => ({ enabled: false, captcha_id: null, image: null, expires_in: 0 }),
    'POST /api/auth/logout': () => ({ message: 'ok' }),
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = String(input).split('?')[0]
      const method = (init?.method ?? 'GET').toUpperCase()
      const handler = routes[`${method} ${url}`]
      if (!handler) throw new Error(`unexpected request: ${method} ${url}`)
      return apiOk(handler())
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
  sessionStorage.clear()
})

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <ToastProvider>
        <LoginPage />
      </ToastProvider>
    </MemoryRouter>,
  )
}

async function waitForForm() {
  await screen.findByLabelText('用户名')
  const form = document.querySelector('form')
  if (!form) throw new Error('登录表单未渲染')
  return form
}

function fill(field: string, value: string) {
  fireEvent.change(screen.getByLabelText(field), { target: { value } })
}

describe('密码登录: user 键随响应换代', () => {
  it('响应带 user ⇒ 写入当前用户(覆盖上一个人的残留)', async () => {
    localStorage.setItem('user', JSON.stringify(A_USER))
    routes['POST /api/auth/login'] = () => ({ token: B_TOKEN, expires_at: EXPIRES, user: B_USER })

    renderLogin()
    const form = await waitForForm()
    fill('用户名', 'bob')
    fill('密码', 'secret123')
    fireEvent.submit(form)

    await waitFor(() => expect(localStorage.getItem('token')).toBe(B_TOKEN))
    expect(JSON.parse(localStorage.getItem('user') || '{}').id).toBe('user-b')
  })

  it('响应不带 user ⇒ 必须清掉上一个人的 user(不能把 B 当成 A)', async () => {
    localStorage.setItem('user', JSON.stringify(A_USER))
    routes['POST /api/auth/login'] = () => ({ token: B_TOKEN, expires_at: EXPIRES })

    renderLogin()
    const form = await waitForForm()
    fill('用户名', 'bob')
    fill('密码', 'secret123')
    fireEvent.submit(form)

    await waitFor(() => expect(localStorage.getItem('token')).toBe(B_TOKEN))
    expect(localStorage.getItem('user')).toBeNull()
  })
})

describe('注册: 响应不带 user 同样不能留旧身份', () => {
  it('注册成功(无 user) ⇒ user 键不保留上一个人', async () => {
    localStorage.setItem('user', JSON.stringify(A_USER))
    routes['POST /api/auth/register'] = () => ({
      message: 'ok',
      token: B_TOKEN,
      expires_at: EXPIRES,
      api_key: null,
    })

    renderLogin()
    await waitForForm()
    // 切到注册 Tab(模式切换按钮带 aria-pressed, 与提交按钮同名'注册'要区分)
    const registerTab = screen
      .getAllByRole('button', { name: '注册' })
      .find(button => button.hasAttribute('aria-pressed'))
    if (!registerTab) throw new Error('注册 Tab 未渲染')
    fireEvent.click(registerTab)

    fill('邮箱', 'bob@example.com')
    fill('验证码', '123456')
    fill('密码', 'secret123')
    fill('确认密码', 'secret123')
    fireEvent.click(screen.getByRole('checkbox'))

    const form = document.querySelector('form')
    if (!form) throw new Error('注册表单未渲染')
    fireEvent.submit(form)

    await waitFor(() => expect(localStorage.getItem('token')).toBe(B_TOKEN))
    expect(localStorage.getItem('user')).toBeNull()
  })
})
