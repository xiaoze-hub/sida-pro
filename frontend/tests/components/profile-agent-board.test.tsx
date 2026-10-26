// @vitest-environment jsdom
//
// 2026-10-10 AI 链路 P1: `GET /profile/stats/accuracy`(分 Agent 命中榜)此前零前端消费。
// 本用例把"命中榜上屏 + 样本不足显式 + 失败显式 + 缺值不编造"钉死。
// mock 的是**网络层**(@panwatch/api.fetchAPI), 组件与 useApiQuery 走真实代码。
import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  fail: null as string | null,
  board: null as unknown,
  stats: {} as unknown,
}))

vi.mock('@panwatch/api', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    fetchAPI: (path: string) => {
      if (api.fail) return Promise.reject(new Error(api.fail))
      if (path === '/profile/stats/accuracy') return Promise.resolve(api.board)
      if (path === '/profile/stats') return Promise.resolve(api.stats)
      // 其余端点(账号信息/通知渠道/API Key 等)不参与本用例, 给中性默认。
      return Promise.resolve({ username: 'u', nickname: 'n', avatar: '', role: 'owner', created_at: null })
    },
  }
})

import { Profile } from '@/pages/Profile'

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <Profile />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  api.fail = null
  api.board = { since: null, scope: 'global', note: '预测记录无用户维度, 按全平台统计', overall: {}, agents: [] }
  api.stats = {
    prediction: { hit_count: 0, total: 0, hit_rate: null, scope: 'global', note: '' },
    watchlist_count: 0,
    position_count: 0,
    has_shadow_profile: false,
  }
})
afterEach(() => cleanup())

describe('个人中心 Agent 命中榜 (AI 链路 P1)', () => {
  it('渲染分 Agent 行: 命中率/命中/平均收益; 样本不足显式不参评', async () => {
    api.board = {
      since: '2026-09-10',
      scope: 'global',
      note: '预测记录无用户维度, 按全平台统计',
      overall: { hit_count: 6, total: 13, hit_rate: 46.2, avg_return_pct: 1.1, scope: 'global' },
      agents: [
        { agent: 'good', hit: 5, total: 6, hit_rate: 83.3, avg_return_pct: 2.0, qualified: true },
        { agent: 'one-shot', hit: 1, total: 1, hit_rate: 100.0, avg_return_pct: 3.0, qualified: false },
      ],
    }
    mount()
    const board = await screen.findByTestId('profile-agent-board')
    const good = within(board).getByTestId('profile-agent-row-good')
    expect(good.textContent).toContain('83.3%')
    expect(good.textContent).toContain('5/6')
    expect(good.textContent).toContain('+2%')
    expect(good.textContent).toContain('参评')
    // 小样本(1/1)显式"样本不足", 不参与排名/不被读成 100% 可靠
    expect(within(board).getByTestId('profile-agent-row-one-shot').textContent).toContain('样本不足')
  })

  it('空样本: 显式空态, 不铺空表', async () => {
    api.board = { since: null, scope: 'global', note: '', overall: {}, agents: [] }
    mount()
    expect(await screen.findByTestId('profile-agent-board-empty')).toBeTruthy()
  })

  it('取数失败: 显式失败文案(原文透传), 不假装成功', async () => {
    api.fail = '查询失败: 后端 500'
    mount()
    const err = await screen.findByTestId('profile-agent-board-error')
    expect(err.textContent).toContain('Agent 命中榜加载失败')
    expect(err.textContent).toContain('查询失败: 后端 500')
  })

  it('hit_rate/avg_return 为 null 时显 --, 不编 0%', async () => {
    api.board = {
      since: null,
      scope: 'global',
      note: '',
      overall: {},
      agents: [{ agent: 'newbie', hit: 0, total: 2, hit_rate: null, avg_return_pct: null, qualified: false }],
    }
    mount()
    const row = await screen.findByTestId('profile-agent-row-newbie')
    expect(row.textContent).toContain('--')
    expect(row.textContent).not.toContain('0%')
  })
})
