// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// A2 命令面板动作化 (2026-09-10): Enter=跳转, Shift+Enter=加自选; 重复添加给显式提示。
const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn(), navigate: vi.fn(), toast: vi.fn() }))
vi.mock('@sida/api', () => ({ fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a) }))
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => mocks.navigate }
})
vi.mock('@sida/base-ui/components/ui/toast', () => ({
  useToast: () => ({ toast: mocks.toast }),
}))

import CommandPalette from '../../src/components/CommandPalette'

const STOCKS = [
  { symbol: '600519', name: '贵州茅台', market: 'CN' },
  { symbol: '00700', name: '腾讯控股', market: 'HK' },
]

function renderPalette(onClose = () => {}) {
  return render(
    <MemoryRouter>
      <CommandPalette open onClose={onClose} />
    </MemoryRouter>,
  )
}

async function searchStocks() {
  const input = screen.getByPlaceholderText(/搜索股票/)
  fireEvent.change(input, { target: { value: '600519' } })
  await waitFor(() => expect(screen.getByText('贵州茅台')).toBeTruthy(), { timeout: 2000 })
  return input
}

beforeEach(() => {
  // 2026-09-20: 面板新增 frecency(最近使用优先, 存 localStorage) ⇒ 用例之间必须隔离,
  // 否则前一个用例"访问过 600519"会把下一个用例的首个条目顶成别的股票(顺序依赖 = 假绿/假红)。
  try {
    localStorage.clear()
  } catch {
    /* jsdom 环境忽略 */
  }
  mocks.fetchAPI.mockReset()
  mocks.navigate.mockReset()
  mocks.toast.mockReset()
})
afterEach(cleanup)

describe('CommandPalette A2 动作化', () => {
  it('搜股结果上普通 Enter = 跳分析页, 不发加自选请求', async () => {
    mocks.fetchAPI.mockResolvedValue(STOCKS)
    renderPalette()
    const input = await searchStocks()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(mocks.navigate).toHaveBeenCalledWith(
      expect.stringContaining('/analysis/600519/'),
      expect.objectContaining({ state: { navBack: { path: '/', label: '首页' } } }),
    )
    const postCalls = mocks.fetchAPI.mock.calls.filter(([, opts]) => (opts as RequestInit | undefined)?.method === 'POST')
    expect(postCalls.length).toBe(0)
  })

  it('Shift+Enter = 加自选: POST /stocks(带 symbol/name/market) + 成功 toast + 关闭面板', async () => {
    mocks.fetchAPI.mockImplementation((path: string, opts?: RequestInit) => {
      if (path.startsWith('/stocks/search')) return Promise.resolve(STOCKS)
      if (path === '/stocks' && opts?.method === 'POST') return Promise.resolve({ id: 1 })
      return Promise.resolve(null)
    })
    const onClose = vi.fn()
    renderPalette(onClose)
    const input = await searchStocks()
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    const post = mocks.fetchAPI.mock.calls.find(([, opts]) => (opts as RequestInit | undefined)?.method === 'POST')
    expect(post?.[0]).toBe('/stocks')
    expect(JSON.parse(String((post?.[1] as RequestInit).body))).toEqual({ symbol: '600519', name: '贵州茅台', market: 'CN' })
    expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('已加自选'), 'success')
    expect(mocks.navigate).not.toHaveBeenCalled()
  })

  it('重复添加(400 已存在) → 提示已在自选, 面板不关不崩', async () => {
    mocks.fetchAPI.mockImplementation((path: string, opts?: RequestInit) => {
      if (path.startsWith('/stocks/search')) return Promise.resolve(STOCKS)
      if (path === '/stocks' && opts?.method === 'POST') return Promise.reject(new Error('股票 600519 已存在'))
      return Promise.resolve(null)
    })
    const onClose = vi.fn()
    renderPalette(onClose)
    const input = await searchStocks()
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })

    await waitFor(() => expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('已在自选'), 'info'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('页命令 Shift+Enter 不触发加自选(仍按跳转处理)', async () => {
    mocks.fetchAPI.mockResolvedValue([])
    renderPalette()
    const input = screen.getByPlaceholderText(/搜索股票/)
    fireEvent.change(input, { target: { value: '数据源' } })
    await waitFor(() => expect(screen.getByText('数据源')).toBeTruthy())
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(mocks.navigate).toHaveBeenCalledWith('/system?tab=datasources')
    const postCalls = mocks.fetchAPI.mock.calls.filter(([, opts]) => (opts as RequestInit | undefined)?.method === 'POST')
    expect(postCalls.length).toBe(0)
  })

  it('底部提示含 ⇧↵ 加自选', () => {
    mocks.fetchAPI.mockResolvedValue([])
    renderPalette()
    expect(screen.getByText(/加自选/)).toBeTruthy()
  })

  // P0 共享导航元数据(2026-11-07): 面板此前对页面命令**零权限过滤** ——
  // 菜单里藏起来的模块, 键盘仍能点进去再吃 403。这里钉住"面板与菜单同一套可见性"。
  it('缺少模块权限的页面不出现在面板(暗盘资金需要 view_dark)', async () => {
    mocks.fetchAPI.mockResolvedValue([])
    render(
      <MemoryRouter>
        <CommandPalette open onClose={() => {}} myPerms={new Set(['view_dashboard', 'view_quotes'])} />
      </MemoryRouter>,
    )
    const input = screen.getByPlaceholderText(/搜索股票/)
    fireEvent.change(input, { target: { value: '暗盘' } })
    await waitFor(() => expect(screen.getByText('无匹配结果')).toBeTruthy())
    expect(screen.queryByText('暗盘资金')).toBeNull()
  })

  it('非 owner 看不到管理后台(即便权限点齐全, ownerOnly 仍按角色挡)', async () => {
    mocks.fetchAPI.mockResolvedValue([])
    render(
      <MemoryRouter>
        <CommandPalette open onClose={() => {}} myPerms={new Set(['manage_users'])} />
      </MemoryRouter>,
    )
    const input = screen.getByPlaceholderText(/搜索股票/)
    fireEvent.change(input, { target: { value: '管理后台' } })
    await waitFor(() => expect(screen.getByText('无匹配结果')).toBeTruthy())
  })

  it('owner 在面板能看到管理后台(角色可见性与侧栏一致)', async () => {
    mocks.fetchAPI.mockResolvedValue([])
    // role=owner 的 JWT: 面板与 App 读同一份 claims(lib/jwt)
    localStorage.setItem('token', `h.${btoa(JSON.stringify({ role: 'owner' }))}.s`)
    render(
      <MemoryRouter>
        <CommandPalette open onClose={() => {}} myPerms={new Set(['manage_users'])} />
      </MemoryRouter>,
    )
    const input = screen.getByPlaceholderText(/搜索股票/)
    fireEvent.change(input, { target: { value: '管理后台' } })
    await waitFor(() => expect(screen.getByText('管理后台')).toBeTruthy())
  })

  it('旧面板的检索词仍能命中(预测 → 行情工作台入口)', async () => {
    mocks.fetchAPI.mockResolvedValue([])
    renderPalette()
    const input = screen.getByPlaceholderText(/搜索股票/)
    fireEvent.change(input, { target: { value: '预测' } })
    await waitFor(() => expect(screen.getAllByText('行情').length).toBeGreaterThan(0))
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(mocks.navigate).toHaveBeenCalledWith('/quotes')
  })
})
