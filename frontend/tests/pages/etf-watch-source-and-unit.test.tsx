// @vitest-environment jsdom
/**
 * ETF 看板 · 分钟来源与 SC8 单位口径（2026-11-10 读侧安全修复）。
 *
 * 三个反例（旧页面的真实错法）：
 * 1. 页头写死「数据源·通达信 TQ」——兜底日（em_trends2）与混源日都会说谎；
 * 2. SC8 用「单日份额变动 ≥100 亿份视为托底日」：100 来自采集器「100 亿元净流入」的
 *    金额阈值，与份额不可换算，属于无依据推断；
 * 3. 把单位写成已确认事实（实际只是推断）。
 * 本用例只断言页面照接口字段展示，不替后端改口径。
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

const supportSeries = [
  { date: '20261015', scale_yi: 32772.84, net_sub_yi: 227.1 },
  { date: '20261016', scale_yi: 32886.32, net_sub_yi: 113.48 },
]

function mockPayloads(intradaySource: Record<string, unknown>) {
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
          baseline_days: 2,
          baseline_days_requested: 20,
          baseline_confirmed: false,
          last_minute: '0931',
          etfs_expected: 20,
          full_minutes: 2,
          strict_minutes: 2,
          min_etfs_for_complete: 20,
          minutes: 2,
        },
        ...intradaySource,
      })
    }
    if (url.startsWith('/etf-watch/support')) {
      return Promise.resolve({
        state: 'ok',
        as_of: '20261016',
        stale: true,
        freshness: { as_of: '20261016', checked_at: '2026-11-10', lag_trading_days: 16, stale: true },
        unit: '亿份',
        net_sub_unit: '亿份',
        unit_confidence: 'inferred',
        unit_evidence:
          '推断依据：交易所份额合计 32863.16 亿份 vs SC8 32886.32（差 -0.07%）；反证：Σ份额×净值 为列0 的 1.42 倍。',
        series: supportSeries,
        sample_count: supportSeries.length,
        latest: supportSeries[1],
        latest_percentile_in_samples: 33.3,
        single_share_supported: false,
        single_share_note: '单只 ETF 净申购不可用',
        note: 'SC8 两列推断为份额（亿份）',
      })
    }
    return Promise.resolve({ state: 'no_data', note: '尚无分钟数据落库。' })
  })
}

describe('EtfWatch 分钟来源与 SC8 口径', () => {
  it('混源日：来源照实列出，基线降级为参考', async () => {
    mockPayloads({
      source: 'mixed',
      sources: ['em_trends2', 'tdx_tq'],
      source_labels: ['东财 trends2', '通达信 TQ'],
      source_by_date: { '20261106': ['东财 trends2', '通达信 TQ'] },
      source_note: '⚠️ 分钟表来源混合（20261106=东财 trends2/通达信 TQ）：混源数据不能当成单一来源口径',
      baseline_sources: ['em_trends2'],
      baseline_source_labels: ['东财 trends2'],
    })
    render(
      <MemoryRouter>
        <EtfWatchPage />
      </MemoryRouter>,
    )
    const sourceBox = await screen.findByLabelText('分钟数据来源')
    expect(sourceBox.textContent).toContain('东财 trends2')
    expect(sourceBox.textContent).toContain('通达信 TQ')
    expect(sourceBox.textContent).toContain('混源')
    // 基线含非 TQ 来源 → 页面必须显式说「只作参考」
    expect(screen.getByText(/基线含非 TQ/)).toBeTruthy()
    // 不许再写死「数据源·通达信 TQ」
    expect(screen.queryByText(/数据源·通达信 TQ/)).toBeNull()
  })

  it('兜底日：页面显示东财 trends2，不冒充通达信 TQ', async () => {
    mockPayloads({
      source: 'em_trends2',
      sources: ['em_trends2'],
      source_labels: ['东财 trends2'],
      source_note: '该数据由东财 trends2 兜底写入（em_trends2），不是 TQ 原生分钟线',
    })
    render(
      <MemoryRouter>
        <EtfWatchPage />
      </MemoryRouter>,
    )
    const sourceBox = await screen.findByLabelText('分钟数据来源')
    expect(sourceBox.textContent).toContain('东财 trends2')
    expect(sourceBox.textContent).not.toContain('通达信')
    expect(screen.queryByText(/数据源·通达信 TQ/)).toBeNull()
  })

  it('SC8：单位标为推断，且没有任何 100 亿份托底阈值文案', async () => {
    mockPayloads({ source: 'tdx_tq', source_labels: ['通达信 TQ'] })
    render(
      <MemoryRouter>
        <EtfWatchPage />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/单位口径为推断/)).toBeTruthy()
    expect(screen.getByText(/1\.42 倍/)).toBeTruthy()
    // 旧「托底日样本数 / ≥100 亿份」整套推断必须消失
    expect(screen.queryByText(/托底日样本数/)).toBeNull()
    expect(screen.queryByText(/≥100/)).toBeNull()
    expect(screen.queryByText(/视为托底日样本/)).toBeNull()
    // 仍保留可证事实：样本交易日数、样本内分位
    expect(screen.getByText('样本交易日数')).toBeTruthy()
    expect(screen.getByText('最新值在样本内分位')).toBeTruthy()
    expect(screen.getByText('33.3%')).toBeTruthy()
    // 单位只出现一次：总值裸数字 + 「亿份（推断口径）」，不能再拼成「… 亿 / 亿份」
    const scaleValue = screen.getAllByText('32886.32')[0]
    const scaleCard = scaleValue.parentElement?.textContent || ''
    expect(scaleCard).toContain('亿份（推断口径）')
    expect(scaleCard).not.toContain('32886.32 亿')
  })
})
