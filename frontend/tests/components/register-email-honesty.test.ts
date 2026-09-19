/**
 * 钉子: 邮件服务不可用时, 注册/验证码登录页要**提前说清楚**(2026-09-19)。
 *
 * 由来: 生产没配 SMTP → 验证码发不出去。旧行为更糟 —— 后端**无条件**返回成功,
 * 页面还弹「验证码已发送, 请注意查收」, 用户只会怀疑自己邮箱。
 * 现在: `/api/auth/status` 给出 `email_configured`, 页面据此提示 + 禁用发送按钮。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const raw = readFileSync(resolve(__dirname, '../../src/pages/Login.tsx'), 'utf-8')
const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
const apiSrc = readFileSync(resolve(__dirname, '../../packages/api/src/auth.ts'), 'utf-8')

describe('注册页: 邮件服务不可用时的诚实提示', () => {
  it('从 /api/auth/status 读 email_configured(不自己猜)', () => {
    expect(apiSrc).toMatch(/email_configured\?: boolean/)
    expect(src).toContain('setEmailReady(data.email_configured !== false)')
  })

  it('只有明确 false 才提示(字段缺失时按可用处理, 不误报)', () => {
    expect(src).toMatch(/useState\(true\)/)
    expect(src).toMatch(/email_configured !== false/)
  })

  it('渲染提示文案, 且指向可行出路', () => {
    expect(src).toContain('邮件服务暂未开通')
    expect(src).toContain('密码登录')
  })

  it('不可用时禁用「发送验证码」(点了必然失败, 别让用户白点)', () => {
    expect(src).toMatch(/disabled=\{countdown > 0 \|\| sending \|\| !emailReady\}/)
  })

  it('提示只出现在邮箱相关模式, 不污染密码登录', () => {
    expect(src).toMatch(/!emailReady && \(isRegister \|\| isEmailCode\)/)
  })
})
