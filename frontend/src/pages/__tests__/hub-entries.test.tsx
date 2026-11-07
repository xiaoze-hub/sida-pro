// @vitest-environment jsdom
import type { ReactNode } from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'

import SettingsHubPage, { SETTINGS_TABS, SettingsRelatedEntries } from '../SettingsHub'
import SystemPage, { SYSTEM_TABS, SystemRelatedEntries } from '../System'

/**
 * 系统/设置两个枢纽页的分工与跨页入口(2026-11-07)。
 *
 * 核查结论(改动前):
 *   · 数据源只在 /system?tab=datasources, 而 /settings 里有一个「数据源凭证」分区(接口 Key),
 *     从设置页找「数据源」会落到凭证上; 两页之间零互链, 只能从侧栏绕。
 *   · 管理后台(/admin)在侧栏有项, 但设置页内嵌的「用户管理」是另一套 UI(同一份账号数据,
 *     另有「Model 权限 / 删除」只有设置版有), 页面里没有任何提示两者同源。
 *
 * 本测试锁三件事:
 *   ① 两个枢纽页都给出可发现的跨页链接, 且 href 就是规范地址(不改旧地址/不改默认落点);
 *   ② 链接可见性与 navigation.ts 同口径 —— manage_datasources / owner + manage_users;
 *   ③ Tab 分工不塌方: 数据源 Tab 仍挂 manage_datasources, 设置页仍是 设置/审计(owner)/帮助 三页签。
 */

const renderWithRouter = (ui: ReactNode) => render(<MemoryRouter>{ui}</MemoryRouter>)

afterEach(cleanup)

describe('System 页: 相关入口', () => {
  it('默认给出「设置」链接, 指向 /settings(不是子路由, 不改变设置页默认 Tab)', () => {
    renderWithRouter(<SystemRelatedEntries />)
    expect(screen.getByRole('link', { name: /设置/ }).getAttribute('href')).toBe('/settings')
  })

  it('ownenr + manage_users 才显示「管理后台」', () => {
    renderWithRouter(<SystemRelatedEntries myPerms={new Set(['manage_users'])} isOwner={() => true} />)
    expect(screen.getByRole('link', { name: /管理后台/ }).getAttribute('href')).toBe('/admin')
  })

  it('非 owner 或无 manage_users 时不显示「管理后台」, 但设置链接仍在', () => {
    renderWithRouter(<SystemRelatedEntries myPerms={new Set(['manage_users'])} isOwner={() => false} />)
    expect(screen.queryByRole('link', { name: /管理后台/ })).toBeNull()
    expect(screen.getByRole('link', { name: /设置/ })).toBeTruthy()
    cleanup()
    renderWithRouter(<SystemRelatedEntries myPerms={new Set(['view_quotes'])} isOwner={() => true} />)
    expect(screen.queryByRole('link', { name: /管理后台/ })).toBeNull()
  })

  it('权限未加载(myPerms=null)时不闪隐, 回退角色判断', () => {
    renderWithRouter(<SystemRelatedEntries myPerms={null} isOwner={() => true} />)
    expect(screen.getByRole('link', { name: /管理后台/ })).toBeTruthy()
  })

  it('页面渲染时入口与既有 Tab 并存, 且默认 Tab 仍是 agents', () => {
    renderWithRouter(<SystemPage myPerms={new Set(['manage_agents', 'manage_datasources', 'manage_users'])} isOwner={() => true} />)
    expect(screen.getByRole('navigation', { name: '相关入口' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: /数据源/ })).toBeTruthy()
  })
})

describe('Settings 页: 相关入口', () => {
  it('「数据源管理」直达 /system?tab=datasources, 并显式说明凭证只是接口 Key', () => {
    renderWithRouter(<SettingsRelatedEntries myPerms={new Set(['manage_datasources'])} isOwner={() => false} />)
    const link = screen.getByRole('link', { name: /数据源管理/ })
    expect(link.getAttribute('href')).toBe('/system?tab=datasources')
    expect(link.textContent).toContain('接口 Key')
  })

  it('无 manage_datasources 时隐藏数据源链接(与 Tab 过滤同口径), 但不连坐管理后台', () => {
    // owner 的后端权限集是全量(permissions.py: _OWNER_PERMS = ALL_PERMISSIONS),
    // 这里只造一个缺 manage_datasources 的集合, 验证两条链接各自独立判定。
    renderWithRouter(<SettingsRelatedEntries myPerms={new Set(['manage_users'])} isOwner={() => true} />)
    expect(screen.queryByRole('link', { name: /数据源管理/ })).toBeNull()
    expect(screen.getByRole('link', { name: /管理后台/ })).toBeTruthy()
  })

  it('两条链接都不可见时整块不渲染, 不留空导航', () => {
    const { container } = renderWithRouter(<SettingsRelatedEntries myPerms={new Set()} isOwner={() => false} />)
    expect(container.querySelector('nav')).toBeNull()
  })

  it('页面渲染时入口存在, 用户管理仍在「设置」Tab 内(不迁移、不新增重复页签)', () => {
    renderWithRouter(<SettingsHubPage myPerms={new Set(['manage_datasources', 'manage_users'])} isOwner={() => true} />)
    expect(screen.getByRole('navigation', { name: '相关入口' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: '设置' })).toBeTruthy()
  })
})

describe('Tab 分工不塌方', () => {
  it('系统页: 数据源仍挂 manage_datasources, Agent 仍挂 manage_agents', () => {
    const byKey = Object.fromEntries(SYSTEM_TABS.map(t => [t.key, t]))
    expect(Object.keys(byKey)).toEqual(['status', 'agents', 'datasources', 'jobs', 'errors'])
    expect(byKey.datasources.perm).toBe('manage_datasources')
    expect(byKey.agents.perm).toBe('manage_agents')
    expect(byKey.status.ownerOnly).toBe(true)
    expect(byKey.jobs.ownerOnly).toBe(true)
    expect(byKey.errors.ownerOnly).toBe(true)
  })

  it('设置页: 仍是 设置/审计/帮助, 审计保持 ownerOnly', () => {
    expect(SETTINGS_TABS.map(t => t.key)).toEqual(['settings', 'audit', 'help'])
    expect(SETTINGS_TABS.find(t => t.key === 'audit')?.ownerOnly).toBe(true)
    expect(SETTINGS_TABS.find(t => t.key === 'help')?.ownerOnly).toBeUndefined()
  })
})
