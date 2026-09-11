// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))
vi.mock('@panwatch/api', () => ({ fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a) }))

import ThemeMoodPage from '@/pages/ThemeMood'

const RESP = {
  trade_date: '20260911',
  window: 20,
  count: 2,
  items: [
    {
      block_code: '881101.SH', block_name: '元件', block_type: 'industry', score: 78.2, delta: 4.1,
      confidence: 86, core: true, s1: 80, s2: 75, s3: 82, s4: 70, s5: 60, limit_up_cnt: 9, max_boards: 3,
      core_stocks: [{ symbol: '600001.SH', name: '甲股', boards: 3, pct: 10.0, score: 88.4, prob: 0.62 }],
      cells: [{ date: '20260910', score: 74.1, limit_up_cnt: 7 }, { date: '20260911', score: 78.2, limit_up_cnt: 9 }],
    },
    {
      block_code: '880301.SH', block_name: '某概念', block_type: 'concept', score: 61.0, delta: -2.0,
      confidence: 60, core: false, s1: 60, s2: 62, s3: null, s4: 50, s5: 55, limit_up_cnt: 3, max_boards: 2,
      core_stocks: [], cells: [{ date: '20260911', score: 61.0, limit_up_cnt: 3 }],
    },
  ],
}

describe('ThemeMood 页面', () => {
  afterEach(() => cleanup())
  beforeEach(() => mocks.fetchAPI.mockReset())

  it('渲染榜单与核心标记', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<ThemeMoodPage />)
    expect((await screen.findAllByText('元件')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('78.2').length).toBeGreaterThan(0)
    expect(screen.getByText('核心')).toBeTruthy()
  })

  it('按 window=20 请求', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<ThemeMoodPage />)
    await screen.findAllByText('元件')
    expect(String(mocks.fetchAPI.mock.calls[0][0])).toContain('window=20')
  })
})
