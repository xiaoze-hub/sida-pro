// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 数智决策 P1-3 UI 契约(2026-10-10) —— 复盘中心(DecisionLedger)只读「当前阈值」小段。
 * 真数据纪律: mock 网络层(decisionsApi/signalsReviewApi), 组件走真实代码, 不编造后端值。
 * 口径: 只读展示生效值 + 来源(default/env); 请求失败显式 `—`, 不 crash、不编造数字。
 */
const mocks = vi.hoisted(() => ({
  stats: vi.fn(),
  log: vi.fn(),
  backtest: vi.fn(),
  entryOutcomes: vi.fn(),
  hitRate: vi.fn(),
  thresholds: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  decisionsApi: {
    stats: (...a: unknown[]) => mocks.stats(...a),
    log: (...a: unknown[]) => mocks.log(...a),
    backtest: (...a: unknown[]) => mocks.backtest(...a),
    entryOutcomes: (...a: unknown[]) => mocks.entryOutcomes(...a),
    thresholds: (...a: unknown[]) => mocks.thresholds(...a),
  },
  signalsReviewApi: {
    hitRate: (...a: unknown[]) => mocks.hitRate(...a),
  },
  kindLabel: (k: string) => k,
}))

import DecisionLedger from '@/pages/DecisionLedger'

beforeEach(() => {
  vi.clearAllMocks()
  mocks.stats.mockResolvedValue({ since: '2026-04-01', min_sample: 30, rows: [], note: '' })
  mocks.log.mockResolvedValue({ count: 0, total: 0, offset: 0, limit: 20, has_more: false, items: [], note: '' })
  mocks.hitRate.mockResolvedValue({ days: 30, by_type: {} })
  mocks.entryOutcomes.mockResolvedValue({ window_days: 30, min_sample: 20, available: true, rows: [], note: '' })
})

afterEach(() => cleanup())

describe('DecisionLedger · 当前阈值只读段(P1-3)', () => {
  it('展示生效值与来源(default / env 标记)', async () => {
    mocks.thresholds.mockResolvedValue({
      items: [
        { key: 'life_line', label: '生命线', value: 1.56, source: 'default', default: 1.56, env: 'SIDA_THRESHOLD_LIFE_LINE' },
        { key: 'strong_line', label: '强势线', value: 4.5, source: 'env', default: 3, env: 'SIDA_THRESHOLD_STRONG_LINE' },
        { key: 'bull_line', label: '大牛线', value: 6, source: 'default', default: 6, env: 'SIDA_THRESHOLD_BULL_LINE' },
      ],
      note: '',
    })
    render(<DecisionLedger />)
    const sec = await screen.findByTestId('thresholds')
    await waitFor(() => expect(sec.textContent).toContain('强势线'))
    expect(sec.textContent).toContain('生命线 1.56')
    expect(sec.textContent).toContain('强势线 4.5(env)') // env 覆盖有标记
    expect(sec.textContent).toContain('大牛线 6')
  })

  it('请求失败不 crash: 段落仍在, 无数字时显式 `—`', async () => {
    mocks.thresholds.mockRejectedValue(new Error('boom'))
    render(<DecisionLedger />)
    const sec = await screen.findByTestId('thresholds')
    await waitFor(() => expect(sec.textContent).toContain('当前阈值(只读)'))
    expect(sec.textContent).toContain('—')
  })
})
