/**
 * 邀请码注册的前端钉子（2026-09-19 内部使用模式）。
 *
 * 后端的三条验收线在 `tests/test_invite_codes.py`（无码被拒 / 用满即失效 / 审计落库）。
 * 这里钉前端那一半：**注册请求必须把邀请码带上**、**按模式渲染**、**管理员区块只打 owner 端点**。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const login = readFileSync(resolve(__dirname, '../../src/pages/Login.tsx'), 'utf-8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')
const adminSection = readFileSync(
  resolve(__dirname, '../../src/components/admin/InviteCodesSection.tsx'),
  'utf-8',
)
const adminPage = readFileSync(resolve(__dirname, '../../src/pages/Admin.tsx'), 'utf-8')
const api = readFileSync(resolve(__dirname, '../../packages/api/src/inviteCodes.ts'), 'utf-8')
const authApi = readFileSync(resolve(__dirname, '../../packages/api/src/auth.ts'), 'utf-8')

describe('注册页: 邀请码', () => {
  it('注册请求带上 invite_code（不带就是白跑一趟）', () => {
    expect(login).toMatch(/invite_code: inviteCode\.trim\(\)/)
  })

  it('邀请制下没填码就本地拦下，不发请求', () => {
    expect(login).toMatch(/registerMode === 'invite' && !inviteCode\.trim\(\)/)
    expect(login).toContain('请填写邀请码')
  })

  it('注册模式来自后端 status，不是前端猜的', () => {
    expect(authApi).toMatch(/register_mode\?: 'invite' \| 'open' \| 'closed'/)
    expect(login).toContain('setRegisterMode(data.register_mode)')
  })

  it('只有 invite 模式才渲染邀请码输入框', () => {
    expect(login).toMatch(/isRegister && registerMode === 'invite' && \(/)
  })

  it('closed 模式隐藏「注册」入口（点了也只会 403）', () => {
    expect(login).toMatch(/registerMode === 'closed' \? \[\] : \[\{ key: 'register'/)
  })

  it('邀请码输入统一大写（与后端 normalize 对齐，避免大小写踩坑）', () => {
    expect(login).toMatch(/setInviteCode\(e\.target\.value\.toUpperCase\(\)\)/)
  })
})

describe('邀请码管理: 只打 owner 端点', () => {
  it('全部走 /admin/invite-codes* 前缀', () => {
    for (const p of [
      "'/admin/invite-codes/mode'",
      '`/admin/invite-codes?limit=',
      "'/admin/invite-codes'",
      '/admin/invite-codes/uses?limit=',
    ]) {
      expect(api).toContain(p)
    }
  })

  it('管理区块挂在 Admin 页里，且有菜单入口', () => {
    expect(adminPage).toContain('InviteCodesSection')
    expect(adminPage).toMatch(/id: 'invite', label: '邀请码管理'/)
  })

  it('审计视图展示"谁/何时/什么 IP"', () => {
    expect(adminSection).toContain('使用人')
    expect(adminSection).toContain('client_ip')
    expect(adminSection).toContain('used_at')
  })

  it('生成后提示"只显示一次"（不把码长期留在界面上）', () => {
    expect(adminSection).toContain('只在这里显示一次')
  })

  it('列表展示已用/可用次数与状态（用尽/停用可区分）', () => {
    expect(adminSection).toContain('used_count')
    expect(adminSection).toContain('已用尽')
    expect(adminSection).toContain('已停用')
  })
})
