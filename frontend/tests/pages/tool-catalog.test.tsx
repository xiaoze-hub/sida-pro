// @vitest-environment jsdom
/**
 * 工具目录页：全量列出、口径逐条、未挂配方显性标注、四态诚实。
 *
 * 口径红线（与页面实现一一对应）：
 *  - 每个工具的口径逐条渲染，**不允许**汇总成一句；
 *  - 未挂配方的工具必须显性标「未挂配方」，不许隐藏（「没人用」本身就是信息）；
 *  - 「读取失败」与「空数据」不许互相冒充。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({
  getChatToolCatalog: vi.fn(),
}))

vi.mock('@/lib/copilot/tools', async () => ({
  ...(await vi.importActual<Record<string, unknown>>('@/lib/copilot/tools')),
  ...api,
}))

import ToolCatalog from '@/pages/copilot/ToolCatalog'
import type { ChatToolCatalog } from '@/lib/copilot/tools'

const CATALOG: ChatToolCatalog = {
  total: 3,
  mounted: 2,
  tools: [
    {
      name: 'get_portfolio',
      description: '获取用户的实盘持仓和模拟盘持仓。',
      caliber: '库内数据: 实盘+模拟盘持仓(本人)',
      requires: 'self',
      charges_budget: true,
      has_fallback: true,
      recipes: ['deep_diagnosis'],
    },
    {
      name: 'tdx_wenda',
      description: '通达信问达搜索。',
      caliber: '通达信问达',
      requires: '',
      charges_budget: true,
      has_fallback: false,
      recipes: [],
    },
    {
      name: 'get_stock_quote',
      description: '获取实时行情。',
      caliber: '多源实时行情聚合',
      requires: '',
      charges_budget: true,
      has_fallback: true,
      recipes: ['intraday_analyst', 'theme_leader'],
    },
  ],
}

beforeEach(() => {
  api.getChatToolCatalog.mockReset()
  api.getChatToolCatalog.mockResolvedValue(CATALOG)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ToolCatalog', () => {
  it('全量列出工具名', async () => {
    render(<ToolCatalog />)
    await waitFor(() => expect(screen.getByText('get_portfolio')).toBeTruthy())
    expect(screen.getByText('tdx_wenda')).toBeTruthy()
    expect(screen.getByText('get_stock_quote')).toBeTruthy()
    expect(api.getChatToolCatalog).toHaveBeenCalledTimes(1)
  })

  it('每个工具的口径逐条展示，不汇总成一句', async () => {
    render(<ToolCatalog />)
    await waitFor(() => expect(screen.getByText('多源实时行情聚合')).toBeTruthy())
    expect(screen.getByText('库内数据: 实盘+模拟盘持仓(本人)')).toBeTruthy()
    expect(screen.getByText('通达信问达')).toBeTruthy()
  })

  it('未挂配方的工具显性标注，而不是隐藏', async () => {
    render(<ToolCatalog />)
    await waitFor(() => expect(screen.getByText('未挂配方')).toBeTruthy())
    expect(screen.getByText('tdx_wenda')).toBeTruthy()
  })

  it('头部统计：总数 / 已挂 / 未挂', async () => {
    render(<ToolCatalog />)
    await waitFor(() => expect(screen.getByText('3 个工具')).toBeTruthy())
    expect(screen.getByText('2 已挂配方')).toBeTruthy()
    expect(screen.getByText('1 未挂配方')).toBeTruthy()
  })

  it('按「未挂配方」过滤后只剩孤儿工具', async () => {
    render(<ToolCatalog />)
    await waitFor(() => expect(screen.getByText('get_portfolio')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '未挂配方' }))
    await waitFor(() => expect(screen.queryByText('get_portfolio')).toBeNull())
    expect(screen.getByText('tdx_wenda')).toBeTruthy()
  })

  it('搜索按口径命中', async () => {
    render(<ToolCatalog />)
    await waitFor(() => expect(screen.getByText('get_portfolio')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('搜索工具'), { target: { value: '通达信' } })
    await waitFor(() => expect(screen.queryByText('get_portfolio')).toBeNull())
    expect(screen.getByText('tdx_wenda')).toBeTruthy()
  })

  it('读取失败透出服务端原文，不用空表格冒充', async () => {
    api.getChatToolCatalog.mockRejectedValue(new Error('注册表不可用'))
    render(<ToolCatalog />)
    await waitFor(() => expect(screen.getByText(/读取工具目录失败/)).toBeTruthy())
    expect(screen.getByText(/注册表不可用/)).toBeTruthy()
  })

  it('读到空数据时提示注册表为空，不是读取失败', async () => {
    api.getChatToolCatalog.mockResolvedValue({ total: 0, mounted: 0, tools: [] })
    render(<ToolCatalog />)
    await waitFor(() => expect(screen.getByText('注册表里没有工具')).toBeTruthy())
  })
})
