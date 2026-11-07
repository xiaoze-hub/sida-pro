// @vitest-environment jsdom
/**
 * 用户管理(设置页入口)停用语义回归 —— 2026-11-07。
 *
 * 背景: 设置页「用户管理」原来的红色 Trash 按钮文案是「删除」, 调的是
 * `authApi.deleteUser`(DELETE /api/auth/users/{id}); /admin 后台用的却是
 * `/users/admin/{uid}/toggle-active`。同一件事两个入口两种说法 + 两种路径,
 * 且 DELETE 在用户眼里等于「数据没了」。
 *
 * 现在两个入口统一: 停用 = is_active=false + token_version+1(踢会话), 数据全部保留,
 * 可随时启用; 已注销(宽限期)账号可恢复; 任何按钮都不再触达硬删除路径。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  toast: vi.fn(),
  confirm: vi.fn(),
}))

vi.mock('@sida/api', () => ({ fetchAPI: mocks.fetchAPI }))
vi.mock('@sida/base-ui/components/ui/toast', () => ({ useToast: () => ({ toast: mocks.toast }) }))

import UserManagement from '@/components/UserManagement'

const ROWS = [
  { id: 'u-owner', username: 'boss', email: 'boss@example.com', role: 'owner', is_active: true, is_deleted: false, deleted_at: null, created_at: '2026-01-01T00:00:00', last_login: null },
  { id: 'u-bob', username: 'bob', email: 'bob@example.com', role: 'member', is_active: true, is_deleted: false, deleted_at: null, created_at: '2026-02-01T00:00:00', last_login: null },
  { id: 'u-carol', username: 'carol', email: null, role: 'pro', is_active: false, is_deleted: false, deleted_at: null, created_at: '2026-03-01T00:00:00', last_login: null },
  { id: 'u-dave', username: 'dave', email: null, role: 'guest', is_active: false, is_deleted: true, deleted_at: '2026-10-30T00:00:00', created_at: '2026-04-01T00:00:00', last_login: null },
]

function renderOwner() {
  render(<UserManagement currentUser={{ id: 'u-owner', username: 'boss', role: 'owner' } as never} />)
}

/** 记录所有写操作(排除列表读取), 便于断言「没有走另一条路径」。 */
function writeCalls() {
  return mocks.fetchAPI.mock.calls.filter(([path]) => path !== '/users/admin/list')
}

beforeEach(() => {
  mocks.fetchAPI.mockReset()
  mocks.toast.mockReset()
  mocks.confirm.mockReset()
  mocks.confirm.mockReturnValue(true)
  mocks.fetchAPI.mockImplementation(async (path: string) => {
    if (path === '/users/admin/list') return { users: ROWS }
    return { ok: true }
  })
  vi.spyOn(window, 'confirm').mockImplementation((msg?: string) => mocks.confirm(msg))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('用户管理 · 停用而非删除', () => {
  it('文案只表达停用: 无「删除」按钮, 停用/已停用/恢复齐全', async () => {
    renderOwner()
    expect(await screen.findByText('bob')).toBeTruthy()

    // 不存在任何「删除」动作(硬删除入口已下线, 只保留 GDPR 自助注销)
    expect(screen.queryByRole('button', { name: /删除/ })).toBeNull()
    // 启用中的用户: 停用按钮
    expect(screen.getByRole('button', { name: '停用 bob' })).toBeTruthy()
    // 已停用用户: 状态徽标 + 启用按钮
    expect(screen.getByText('已停用')).toBeTruthy()
    expect(screen.getByRole('button', { name: '启用 carol' })).toBeTruthy()
    // 已注销(宽限期)用户: 可恢复
    expect(screen.getByText('已注销（宽限期，可恢复）')).toBeTruthy()
    expect(screen.getByRole('button', { name: '恢复 dave' })).toBeTruthy()
    // 说明文案明确「数据全部保留」
    expect(screen.getByText(/数据全部保留/)).toBeTruthy()
  })

  it('停用走 toggle-active(is_active=false), 绝不调 DELETE; 确认框说明数据保留', async () => {
    renderOwner()
    fireEvent.click(await screen.findByRole('button', { name: '停用 bob' }))

    await waitFor(() => expect(writeCalls()).toHaveLength(1))
    const [path, init] = writeCalls()[0]
    expect(path).toBe('/users/admin/u-bob/toggle-active')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({ is_active: false })

    // 没有触达硬删除路径
    expect(mocks.fetchAPI.mock.calls.some(([p]) => String(p).startsWith('/auth/users/'))).toBe(false)
    expect(writeCalls().some(([, i]) => i?.method === 'DELETE')).toBe(false)

    // 确认文案必须讲清「无法登录」+「数据保留/可恢复」
    const prompt = String(mocks.confirm.mock.calls[0]?.[0] ?? '')
    expect(prompt).toContain('bob')
    expect(prompt).toContain('无法登录')
    expect(prompt).toContain('全部保留')
    expect(prompt).toContain('重新启用')

    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith('已停用 bob(数据保留, 可随时启用)', 'success'))
  })

  it('取消确认时不发任何写请求', async () => {
    mocks.confirm.mockReturnValue(false)
    renderOwner()
    fireEvent.click(await screen.findByRole('button', { name: '停用 bob' }))
    await new Promise(r => setTimeout(r, 0))
    expect(writeCalls()).toHaveLength(0)
  })

  it('启用已停用用户走同一端点且 is_active=true, 不需要确认', async () => {
    renderOwner()
    fireEvent.click(await screen.findByRole('button', { name: '启用 carol' }))

    await waitFor(() => expect(writeCalls()).toHaveLength(1))
    const [path, init] = writeCalls()[0]
    expect(path).toBe('/users/admin/u-carol/toggle-active')
    expect(JSON.parse(String(init?.body))).toEqual({ is_active: true })
    expect(mocks.confirm).not.toHaveBeenCalled()
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith('已启用 carol', 'success'))
  })

  it('已注销账号可恢复: 命中原 restore 端点', async () => {
    renderOwner()
    fireEvent.click(await screen.findByRole('button', { name: '恢复 dave' }))

    await waitFor(() => expect(writeCalls()).toHaveLength(1))
    expect(writeCalls()[0][0]).toBe('/users/admin/u-dave/restore')
    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith('已恢复 dave', 'success'))
  })

  it('多角色: owner 行不出现停用/重置入口, 只对非 owner 暴露', async () => {
    renderOwner()
    await screen.findByText('boss')
    expect(screen.queryByRole('button', { name: '停用 boss' })).toBeNull()
    expect(screen.queryByRole('button', { name: '重置 boss 的密码' })).toBeNull()
    // 非 owner 角色(成员/Pro/访客)都各自有停用/启用入口
    expect(screen.getByRole('button', { name: '停用 bob' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '启用 carol' })).toBeTruthy()
  })

  it('非 owner 看不到用户管理, 也不打用户列表接口', () => {
    render(<UserManagement currentUser={{ id: 'u-bob', username: 'bob', role: 'member' } as never} />)
    expect(screen.getByText('仅管理员可管理用户')).toBeTruthy()
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
  })
})
