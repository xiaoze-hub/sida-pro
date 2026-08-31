import { fetchAPI } from './client'

export interface AuthStatus {
  initialized: boolean
  multi_user?: boolean
  user?: UserInfo | null
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
  me: () => fetchAPI<{ user: UserInfo }>('/auth/me'),
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
