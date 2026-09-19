import { fetchAPI } from './client'

export interface AuthStatus {
  initialized: boolean
  multi_user?: boolean
  user?: UserInfo | null
  /** 邮件服务是否已配置(2026-09-19): false 时邮箱注册/验证码登录发不出验证码 —— 前端要明说 */
  email_configured?: boolean
  /** 注册模式(2026-09-19): invite=需邀请码(内部使用默认) / open / closed */
  register_mode?: 'invite' | 'open' | 'closed'
}

export interface UserInfo {
  id: string
  username: string
  role: 'owner' | 'member' | 'guest'
  is_active?: boolean
  created_at?: string | null
}

export interface AuthTokenPayload {
  token: string
  expires_at: string
  user?: UserInfo | null
}

export interface LoginPayload {
  username: string
  password: string
}

export interface SubscriptionItem {
  report_type: string
  enabled: boolean
  label: string
}

export const authApi = {
  status: () => fetchAPI<AuthStatus>('/auth/status'),
  login: (payload: LoginPayload) =>
    fetchAPI<AuthTokenPayload>('/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  // 邮箱验证码(2026-09-16)
  sendCode: (email: string, purpose: 'register' | 'login') =>
    fetchAPI<{ sent: boolean; expire_in: number }>('/auth/send-code', {
      method: 'POST',
      body: JSON.stringify({ email, purpose }),
    }),
  loginByEmail: (email: string, code: string) =>
    fetchAPI<AuthTokenPayload>('/auth/login-by-email', {
      method: 'POST',
      body: JSON.stringify({ email, code }),
    }),
  me: () => fetchAPI<{ user: UserInfo }>('/auth/me'),
  // P0(2026-09-18): 登出清 httpOnly Cookie(前端 logout() 会调用)
  logout: () =>
    fetchAPI<{ message: string }>('/auth/logout', {
      method: 'POST',
    }),
  changePassword: (oldPassword: string, newPassword: string) =>
    fetchAPI<{ message: string }>('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  // 用户管理(仅 owner)
  listUsers: () => fetchAPI<{ users: UserInfo[] }>('/auth/users'),
  createUser: (payload: { username: string; password: string; role?: string }) =>
    fetchAPI<{ user: UserInfo }>('/auth/users', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateUser: (id: string, payload: { password?: string; role?: string; is_active?: boolean }) =>
    fetchAPI<{ user: UserInfo }>(`/auth/users/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteUser: (id: string) =>
    fetchAPI<{ message: string }>(`/auth/users/${id}`, {
      method: 'DELETE',
    }),
  // 定时报告订阅
  listSubscriptions: () =>
    fetchAPI<{ subscriptions: SubscriptionItem[] }>('/subscriptions/subscriptions'),
  updateSubscription: (reportType: string, enabled: boolean) =>
    fetchAPI<{ report_type: string; enabled: boolean }>(
      `/subscriptions/subscriptions/${reportType}`,
      {
        method: 'PUT',
        body: JSON.stringify({ report_type: reportType, enabled }),
      },
    ),
}
