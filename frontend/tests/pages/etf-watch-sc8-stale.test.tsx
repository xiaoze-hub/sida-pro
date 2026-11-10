// @vitest-environment jsdom
/**
 * ETF 看板 · SC8 份额口径与停更提示（2026-11-10 安全修复）。
 *
 * 两个不许退回的错：
 * 1. SC8 两列是**份额（亿份）**，页面上不许再出现「净申购（亿元）/ETF 规模（亿元）」；
 * 2. TQ SC8 已停更（库内止于 2026-10-16）。接口给 stale/as_of 时，页面必须显示
 *    真实日期 + 滞后交易日数，并明确「不是当前读数」—— 不许把停更日当今天。
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

const INTRADAY = {
  date: '20261110',
  phase: 'live',
  session_phase: 'live',
  state: 'normal',
  series: [
    { minute: '0930', amount_yi: 1, cum_yi: 1, baseline_cum_yi: 0.9, complete: true },
  ],
  totals: {
    cum_yi: 1,
    baseline_avg_yi: 0.9,
    ratio_vs_baseline: 1.1,
    baseline_days: 5,
    baseline_days_requested: 20,
    last_minute: '0930',
    etfs_expected: 20,
    full_minutes: 1,
    min_etfs_for_complete: 16,
    minutes: 1,
  },
}

const STALE_SUPPORT = {
  state: 'stale',
  stale: true,
  as_of: '20261016',
  unit: '亿份',
  net_sub_unit: '亿份',
  scale_unit: '亿份',
  unit_confidence: 'inferred',
  unit_evidence: '推断依据：交易所份额合计对拍 -0.07%；反证：Σ份额×净值 为列0 的 1.42 倍。',
  freshness: {
    as_of: '20261016',
    checked_at: '2026-11-10',
    lag_trading_days: 16,
    max_lag_trading_days: 1,
    verified: true,
    stale: true,
  },
  series: [{ date: '20261016', scale_yi: 32886.32, net_sub_yi: 113.48 }],
  sample_count: 915,
  latest: { date: '20261016', scale_yi: 32886.32, net_sub_yi: 113.48 },
  latest_percentile_in_samples: 100,
  single_share_supported: false,
  single_share_note: '单只 ETF 净申购不可用（全市场 SC8 份额口径）。',
}

const FRESH_SUPPORT = {
  ...STALE_SUPPORT,
  state: 'ok',
  stale: false,
  as_of: '20261109',
  freshness: {
    as_of: '20261109',
    checked_at: '2026-11-10',
    lag_trading_days: 0,
    max_lag_trading_days: 1,
    verified: true,
    stale: false,
  },
  series: [{ date: '20261109', scale_yi: 32900.1, net_sub_yi: -20.0 }],
  latest: { date: '20261109', scale_yi: 32900.1, net_sub_yi: -20.0 },
}

function mockPayloads(support: unknown) {
  api.fetchAPI.mockImplementation((url: string) => {
    if (url.startsWith('/etf-watch/intraday')) return Promise.resolve(INTRADAY)
    if (url.startsWith('/etf-watch/support')) return Promise.resolve(support)
    return Promise.resolve({ state: 'no_data', note: '尚无分钟数据落库。' })
  })
}

function renderPage() {
  return render(
    <MemoryRouter>
      <EtfWatchPage />
    </MemoryRouter>,
  )
}

describe('EtfWatch · SC8 份额口径与停更提示', () => {
  it('停更数据必须写真实日期与滞后交易日，并说明不是当前读数', async () => {
    mockPayloads(STALE_SUPPORT)
    renderPage()
    expect(await screen.findByText(/数据停在 20261016/)).toBeTruthy()
    expect(screen.getByText(/已滞后 16 个交易日/)).toBeTruthy()
    expect(screen.getByText(/不是当前读数/)).toBeTruthy()
    // 真实日期照实展示，不许改写成今天
    expect(screen.queryByText(/数据停在 20261110/)).toBeNull()
  })

  it('份额口径：页面不再出现「净申购（亿元）」「ETF 规模（亿元）」', async () => {
    mockPayloads(STALE_SUPPORT)
    renderPage()
    await screen.findByText(/数据停在 20261016/)
    expect(screen.queryByText(/净申购（亿元）/)).toBeNull()
    expect(screen.queryByText(/ETF 规模（亿元）/)).toBeNull()
    expect(screen.getAllByText(/份额变动（亿份）/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/总份额（亿份）/).length).toBeGreaterThan(0)
    // 旧「100 亿元/100 亿份」托底阈值整套推断必须消失：单位只是推断，不筛样本
    expect(screen.queryByText(/100 亿元/)).toBeNull()
    expect(screen.queryByText(/托底日样本数/)).toBeNull()
    expect(screen.queryByText(/≥100/)).toBeNull()
    expect(screen.getByText(/单位口径为推断/)).toBeTruthy()
  })

  it('数据新鲜时不显示停更提示，但仍按份额口径渲染', async () => {
    mockPayloads(FRESH_SUPPORT)
    renderPage()
    expect(await screen.findByText(/份额变动（亿份）/)).toBeTruthy()
    expect(screen.queryByText(/数据停在/)).toBeNull()
    expect(screen.queryByText(/净申购（亿元）/)).toBeNull()
  })
})
