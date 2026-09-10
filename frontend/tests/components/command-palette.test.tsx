// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// A2 命令面板动作化 (2026-09-10): Enter=跳转, Shift+Enter=加自选; 重复添加给显式提示。
const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn(), navigate: vi.fn(), toast: vi.fn() }))
vi.mock('@panwatch/api', () => ({ fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a) }))
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => mocks.navigate }
})
vi.mock('@panwatch/base-ui/components/ui/toast', () => ({
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
    expect(mocks.navigate).toHaveBeenCalledWith(expect.stringContaining('/analysis/600519/'))
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
})
