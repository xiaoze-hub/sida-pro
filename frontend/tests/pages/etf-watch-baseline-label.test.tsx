// @vitest-environment jsdom
/**
 * ETF 看板 · 基线样本量文案真实性（2026-11-07 二审）。
 *
 * 旧实现把「最近 20 个有数据交易日」写死在 CumChart 的图例与 aria-label 上：
 * 实际只有 2 个有效基线日时，屏幕阅读器与图例都在宣称 20 日 —— 虚报样本量。
 * 现在基线天数只按 payload 的 baseline_days / baseline_days_requested 说话，最多 20 日。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))
vi.mock('@sida/api', () => ({ fetchAPI: api.fetchAPI }))

import EtfWatchPage from '@/pages/EtfWatch'

afterEach(() => {
  cleanup()
  api.fetchAPI.mockReset()
})

const series = [
  { minute: '0930', amount_yi: 1, cum_yi: 1, baseline_cum_yi: 0.5, complete: true },
  { minute: '0931', amount_yi: 2, cum_yi: 3, baseline_cum_yi: 1.5, complete: true },
]

function mockPayloads(baselineDays: number, requested = 20) {
  api.fetchAPI.mockImplementation((url: string) => {
    if (url.startsWith('/etf-watch/intraday')) {
      return Promise.resolve({
        date: '20261106',
        phase: 'closed',
        session_phase: 'closed',
        state: 'normal',
        series,
        totals: {
          cum_yi: 3,
          baseline_avg_yi: 1.5,
          ratio_vs_baseline: 2,
          baseline_days: baselineDays,
          baseline_days_requested: requested,
          last_minute: '0931',
          etfs_expected: 20,
          full_minutes: 2,
          strict_minutes: 2,
          min_etfs_for_complete: 16,
          minutes: 2,
        },
      })
    }
    if (url.startsWith('/etf-watch/support')) {
      return Promise.resolve({ state: 'no_data', note: '尚无落库数据' })
    }
    return Promise.resolve({ state: 'no_data', note: '尚无分钟数据落库。' })
  })
}

describe('EtfWatch 基线样本文案', () => {
  it('只有 2 个有效基线日时，图例与 ARIA 不得声称 20 日', async () => {
    mockPayloads(2)
    render(
      <MemoryRouter>
        <EtfWatchPage />
      </MemoryRouter>,
    )
    const chart = await screen.findByRole('img', { name: /同期基线对比/ })
    const label = chart.getAttribute('aria-label') || ''
    expect(label).toContain('实际 2/20 个交易日')
    expect(label).not.toContain('过去 20 日')
    expect(screen.getByText(/同期基线（实际 2\/20 个交易日，样本不足 20 日）/)).toBeTruthy()
    expect(screen.queryByText(/最近 20 个有数据交易日/)).toBeNull()
  })

  it('样本满 20 日时也不写「最近 20 个有数据交易日」这种与样本绑死的措辞', async () => {
    mockPayloads(20)
    render(
      <MemoryRouter>
        <EtfWatchPage />
      </MemoryRouter>,
    )
    const chart = await screen.findByRole('img', { name: /同期基线对比/ })
    expect(chart.getAttribute('aria-label') || '').toContain('实际 20/20 个交易日')
    expect(screen.queryByText(/最近 20 个有数据交易日/)).toBeNull()
  })
})
