// @vitest-environment jsdom
//
// 2026-09-14 个人中心走查 B: 「账号 / 角色 / 注册时间」在拉取失败时曾渲染成一排 '--'。
// 已登录用户的 username/role/created_at 恒存在 ⇒ 这三行没有合法空态;
// 拉取失败必须给出可见故障态 + 重试, 且失败文案用后端/传输层原文。
// 本文件的作用: 把"失败静默成 --"钉死(去掉故障态分支 ⇒ 第 1 例必红)。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fail: null as string | null, resp: null as unknown }))
vi.mock('@panwatch/api', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    fetchAPI: () =>
      api.fail ? Promise.reject(new Error(api.fail)) : Promise.resolve(api.resp),
  }
})

import { Profile } from '@/pages/Profile'

const PROFILE = {
  username: 'tianxiang',
  nickname: '老板',
  avatar: '',
  role: 'owner',
  created_at: '2026-01-02T03:04:05',
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={client}>
      <Profile />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  api.fail = null
  api.resp = null
})
afterEach(() => cleanup())

describe('个人中心 当前账号 (走查 B)', () => {
  it('拉取失败: 显式故障态 + 重试, 不铺一排 --', async () => {
    api.fail = '查询失败: 后端 500'
    mount()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('账号信息加载失败')
    expect(alert.textContent).toContain('查询失败: 后端 500') // 原文透传
    // 关键: 三个字段标签都不许出现(否则就是"没拉到"被渲染成"本来就是空")
    expect(screen.queryByText('账号')).toBeNull()
    expect(screen.queryByText('角色')).toBeNull()
    expect(screen.queryByText('注册时间')).toBeNull()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()
  })

  it('拉取失败后点重试成功 → 真实值上屏(不再停留在故障态)', async () => {
    api.fail = '服务不可用'
    mount()
    await screen.findByRole('alert')
    api.fail = null
    api.resp = PROFILE
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('tianxiang')).toBeTruthy()
    expect(screen.getByText('管理员')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('拉取成功: 账号 / 角色 / 注册时间 三行都是真值(不出现 --)', async () => {
    api.resp = PROFILE
    mount()
    expect(await screen.findByText('tianxiang')).toBeTruthy()
    expect(screen.getByText('管理员')).toBeTruthy()
    const row = screen.getByText('注册时间').parentElement
    const value = row?.querySelector('span:last-child')?.textContent ?? ''
    expect(value).toMatch(/01\/02/) // formatDateTime 周年月日
    expect(value).not.toBe('--')
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
  })
})
