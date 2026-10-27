// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 自选池信号质量只读卡(2026-10-11 胜率证据层 ②)UI 契约。
 * 真数据纪律: mock 网络层, 组件走真实代码, 不编造后端值。
 * 口径: 样本不足 → 显示 '--'(不给数字); available=false → 显式原因; 不含建议文案。
 */
const mocks = vi.hoisted(() => ({
  stats: vi.fn(),
  log: vi.fn(),
  backtest: vi.fn(),
  entryOutcomes: vi.fn(),
  hitRate: vi.fn(),
  thresholds: vi.fn(),
  watchlistQuality: vi.fn(),
}))

vi.mock('@sida/api', () => ({
  decisionsApi: {
    stats: (...a: unknown[]) => mocks.stats(...a),
    log: (...a: unknown[]) => mocks.log(...a),
    backtest: (...a: unknown[]) => mocks.backtest(...a),
    entryOutcomes: (...a: unknown[]) => mocks.entryOutcomes(...a),
    thresholds: (...a: unknown[]) => mocks.thresholds(...a),
    watchlistQuality: (...a: unknown[]) => mocks.watchlistQuality(...a),
  },
  signalsReviewApi: { hitRate: (...a: unknown[]) => mocks.hitRate(...a) },
  kindLabel: (k: string) => (k === 'resonance3' ? '三指标共振' : k),
}))

import DecisionLedger from '@/pages/DecisionLedger'

beforeEach(() => {
  vi.clearAllMocks()
  mocks.stats.mockResolvedValue({ since: '2026-04-01', min_sample: 30, rows: [], note: '' })
  mocks.log.mockResolvedValue({ count: 0, total: 0, offset: 0, limit: 20, has_more: false, items: [], note: '' })
  mocks.hitRate.mockResolvedValue({ days: 30, by_type: {} })
  mocks.entryOutcomes.mockResolvedValue({ window_days: 30, min_sample: 20, available: true, rows: [], note: '' })
  mocks.thresholds.mockResolvedValue({ items: [], note: '' })
})

afterEach(() => cleanup())

const HZ = (n: number, rate: number | null, ins: boolean, note = '') => ({ n, hit_rate: rate, insufficient: ins, note })

describe('DecisionLedger · 自选池信号质量(2026-10-11 ②)', () => {
  it('样本不足显示 --, 样本足够显示命中率, 且带 regime 标注', async () => {
    mocks.watchlistQuality.mockResolvedValue({
      available: true,
      user_id: 'u1',
      days: 180,
      min_sample: 30,
      symbols_count: 3,
      signals: [
        {
          signal_kind: 'resonance3',
          n_total: 40,
          symbols_count: 3,
          horizons: { t1: HZ(40, 0.6, false), t3: HZ(5, null, true, '样本不足(5 < 30)'), t5: HZ(0, null, true) },
          by_symbol: [],
          regime_buckets: [
            { regime: 'ferment', label: '发酵', n_total: 40, horizons: { t1: HZ(40, 0.6, false), t3: HZ(0, null, true), t5: HZ(0, null, true) } },
          ],
          regime_note: '该信号仅在『发酵』期样本足够(其余状态样本不足, 暂无法比较)',
        },
      ],
      note: '纯账本聚合(零预测)',
    })
    render(<DecisionLedger />)
    const sec = await screen.findByTestId('watchlist-signal-quality')
    await waitFor(() => expect(sec.textContent).toContain('三指标共振'))
    expect(sec.textContent).toContain('60.0%')          // T+1 有样本
    expect(sec.textContent).toContain('发酵')             // regime 分桶
    expect(sec.textContent).toContain('样本不足')          // 不足档标注
    expect(sec.textContent).not.toContain('建议买入')
  })

  it('无法隔离/空池: 显式原因, 不造数字', async () => {
    mocks.watchlistQuality.mockResolvedValue({
      available: false,
      user_id: 'u1',
      days: 180,
      min_sample: 30,
      symbols_count: 0,
      signals: [],
      error: '自选池为空',
      note: '',
    })
    render(<DecisionLedger />)
    const sec = await screen.findByTestId('watchlist-signal-quality')
    await waitFor(() => expect(sec.textContent).toContain('自选池为空'))
  })
})
