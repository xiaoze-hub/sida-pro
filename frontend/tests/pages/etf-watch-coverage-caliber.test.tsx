// @vitest-environment jsdom
/**
 * ETF 看板 · 覆盖口径文案（2026-11-10 复审）。
 *
 * 修的是两个「静默算错」的读侧口径：
 * 1. 旧口径把 16/20 只（80% 闸门）当成可用分钟 —— 16 只的合计天然少 4 只的钱，
 *    同一笔成交额会被算成 1.25 倍「偏活跃」。页面因此不许再出现「≥16 只」这种口径。
 * 2. 基线只收「同日 20/20 只齐备 + 同一批分钟」；没有可比样本时给「不给结论」，
 *    并且把被剔除的原因（篮子缺只 / 缺分钟）逐条摊开，而不是只说「基线不足」。
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
  { minute: '0930', amount_yi: 5, cum_yi: 5, baseline_cum_yi: null, complete: true },
  { minute: '0931', amount_yi: 5, cum_yi: 10, baseline_cum_yi: null, complete: true },
]

function mockIntraday(intraday: Record<string, unknown>) {
  api.fetchAPI.mockImplementation((url: string) => {
    if (url.startsWith('/etf-watch/intraday')) return Promise.resolve(intraday)
    if (url.startsWith('/etf-watch/support')) {
      return Promise.resolve({ state: 'no_data', note: '尚无落库数据' })
    }
    return Promise.resolve({ state: 'no_data', note: '尚无分钟数据落库。' })
  })
}

const common = {
  date: '20261106',
  phase: 'closed',
  session_phase: 'live',
  session_time: '10:00',
  series,
  totals: {
    baseline_days_requested: 20,
    last_minute: '0931',
    etfs_expected: 20,
    min_etfs_for_complete: 20,
    full_minutes: 2,
    strict_minutes: 2,
    minutes: 2,
    coverage: 1,
  },
}

describe('EtfWatch 覆盖口径', () => {
  it('18 日 16/20 的基线被剔除时：不给结论 + 如实报剔除原因，不出现 ≥16 只口径', async () => {
    mockIntraday({
      ...common,
      state: 'no_baseline',
      conclusive: false,
      totals: {
        ...common.totals,
        cum_yi: 10,
        baseline_avg_yi: null,
        ratio_vs_baseline: null,
        withheld_reasons: ['baseline_no_sample'],
        baseline_days: 0,
        baseline_days_dropped: 19,
        baseline_days_incomplete_basket: 19,
        baseline_minutes_compared: 2,
        baseline_missing_days: 1,
      },
    })

    render(
      <MemoryRouter>
        <EtfWatchPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText('基线不足・不给结论')).toBeTruthy()
    expect(screen.getByText(/无同期基线可比，本次不给活跃度结论/)).toBeTruthy()
    expect(screen.getByText(/19 日因篮子缺只被剔除/)).toBeTruthy()
    expect(screen.getAllByText(/20\/20 只齐备/).length).toBeGreaterThan(0)
    // 旧口径（≥16/20 只 / 80% 闸门）不许再出现在页面上
    expect(document.body.textContent).not.toContain('≥16')
    expect(document.body.textContent).not.toContain('偏活跃')
    // 没有可比基线时不许显示比值
    expect(document.body.textContent).not.toContain('1.25 倍')
  })

  it('今日缺只/缺分钟：逐条列出不给结论的原因与覆盖明细', async () => {
    mockIntraday({
      ...common,
      state: 'insufficient',
      conclusive: false,
      totals: {
        ...common.totals,
        cum_yi: 10,
        baseline_avg_yi: null,
        ratio_vs_baseline: null,
        withheld_reasons: ['minutes_short_basket', 'minute_gaps'],
        baseline_days: 5,
        baseline_days_dropped: 0,
        baseline_minutes_compared: 2,
        minutes_short_basket: 1,
        minutes_without_amount: 0,
        minute_gaps: 1,
        minute_gaps_detail: ['0931'],
        coverage: 0.5,
        complete_minutes: 1,
        minutes: 2,
      },
    })

    render(
      <MemoryRouter>
        <EtfWatchPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText('数据不全・不给结论')).toBeTruthy()
    expect(screen.getByText('有分钟篮子缺只（未达全篮子 20/20）')).toBeTruthy()
    expect(screen.getByText('已采集分钟之间有缺口（缺一分钟就让累计少一截）')).toBeTruthy()
    expect(screen.getByText(/1 分钟篮子缺只/)).toBeTruthy()
    expect(screen.getByText(/缺 1 根分钟（0931）/)).toBeTruthy()
    expect(screen.getByText(/只代表采到的部分，不是全天成交额/)).toBeTruthy()
  })
})
