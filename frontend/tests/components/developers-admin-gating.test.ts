/**
 * 钉子: 公开的开发者文档**不许列出管理员端点**(2026-09-19 用户报障)。
 *
 * 由来: `/developers` 是公开面(匿名可见), 但 API 参考里列了 12 个 `auth: 'owner'` 端点
 * (全部用户列表 / 改角色 / 全部 API Key / 冻结 Key / 用量报表 / 轮换 JWT 密钥 / 申请审核…)。
 * 那不是给普通用户看的内部面 —— 既是信息暴露, 也让公开文档看起来像内部工具。
 *
 * 做法: 按角色过滤(非 owner 不渲染 owner 条目), 并给一行说明。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const raw = readFileSync(resolve(__dirname, '../../src/pages/Developers.tsx'), 'utf-8')
const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

describe('开发者文档: 管理员端点不对非管理员展示', () => {
  it('渲染时按角色过滤 owner 条目', () => {
    expect(src).toMatch(/isOwner \? g\.apis : g\.apis\.filter\(\(a\) => a\.auth !== 'owner'\)/)
  })

  it('过滤后空组不渲染(不留空标题)', () => {
    expect(src).toMatch(/\.filter\(\(g\) => g\.apis\.length > 0\)/)
  })

  it('角色来自真实的权限接口, 不是前端猜的', () => {
    expect(src).toContain('getMyPermissions')
    expect(src).toMatch(/perms\?\.role === 'owner'/)
    expect(src).toContain('isAuthenticated')
  })

  it('非管理员能看到"管理员端点仅管理员可见"的说明(不静默隐藏)', () => {
    expect(src).toContain('仅对管理员账号显示')
    expect(src).toMatch(/\{!isOwner && \(/)
  })

  it('数据结构本身仍保留 owner 条目标记(过滤在渲染层, 不在数据层删)', () => {
    // 数据层删掉的话, owner 登录后也看不到了
    expect(src).toMatch(/auth: 'owner'/)
  })
})
