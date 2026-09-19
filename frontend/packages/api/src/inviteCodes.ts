/**
 * 邀请码管理（内部使用模式，2026-09-19）。
 *
 * 产品判定内部使用后，注册改邀请制：**准入权收在管理员手里**。
 * 后端端点全部 owner only（`/api/admin/invite-codes*`），普通用户请求会被 401/403 —— 前端不要
 * 靠"隐藏入口"来当权限，真权限在后端。
 */
import { fetchAPI } from './client'

export type RegisterMode = 'invite' | 'open' | 'closed'

export interface InviteCodeItem {
  code: string
  note: string
  max_uses: number
  used_count: number
  remaining: number
  expires_at: string | null
  disabled: boolean
  created_by: string
  created_at: string
}

export interface InviteCodeUse {
  code: string
  user_id: number | null
  username: string
  client_ip: string
  used_at: string
}

export interface InviteModeInfo {
  mode: RegisterMode
  total: number
  active: number
  used_total: number
}

export const inviteCodesApi = {
  mode: () => fetchAPI<InviteModeInfo>('/admin/invite-codes/mode'),
  list: (limit = 100) =>
    fetchAPI<{ items: InviteCodeItem[]; mode: RegisterMode }>(`/admin/invite-codes?limit=${limit}`),
  create: (payload: { note?: string; max_uses?: number; expires_in_days?: number | null; code?: string }) =>
    fetchAPI<InviteCodeItem>('/admin/invite-codes', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  disable: (code: string) =>
    fetchAPI<{ disabled: boolean; code: string }>(
      `/admin/invite-codes/${encodeURIComponent(code)}/disable`,
      { method: 'POST' },
    ),
  /** 使用审计：谁、什么时候、什么 IP 用了哪个码 */
  uses: (code?: string, limit = 100) =>
    fetchAPI<{ items: InviteCodeUse[] }>(
      `/admin/invite-codes/uses?limit=${limit}${code ? `&code=${encodeURIComponent(code)}` : ''}`,
    ),
}
