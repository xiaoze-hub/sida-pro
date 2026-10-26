// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 数智决策 P2 合集 UI 契约(2026-10-10) —— 复盘中心(DecisionLedger)四处「有功能没入口」补齐:
 *  ① 信号对账卡片(consumes `GET /api/signals/hit-rate`);
 *  ② 入场候选后验卡片(`GET /api/decisions/entry-outcomes`), 样本不足显式;
 *  ③ 共振回测入口(`GET /api/decisions/backtest`), **basis 口径标记必须上屏**, 不可用显式;
 *  ④ 账本明细分页(offset 翻页)。
 * 真数据纪律: mock 的是网络层(decisionsApi/signalsReviewApi), 组件走真实代码, 不编造后端值。
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

const emptyStats = { since: '2026-04-01', min_sample: 30, rows: [], note: '' }
const emptyHitRate = { days: 30, by_type: {} }
const emptyOutcomes = {
  window_days: 30,
  min_sample: 20,
  available: true,
  rows: [],
  note: '胜率 = 后验收益 > 0; 样本不足不给数字。',
}
const emptyLog = { count: 0, total: 0, offset: 0, limit: 20, has_more: false, items: [], note: '' }

function primeDefaults() {
  mocks.stats.mockResolvedValue(emptyStats)
  mocks.log.mockResolvedValue(emptyLog)
  mocks.hitRate.mockResolvedValue(emptyHitRate)
  mocks.entryOutcomes.mockResolvedValue(emptyOutcomes)
  mocks.thresholds.mockResolvedValue({ items: [], note: '' })
}

beforeEach(() => {
  vi.clearAllMocks()
  primeDefaults()
})

afterEach(() => cleanup())

describe('DecisionLedger · 信号对账卡片(P2-2)', () => {
  it('渲染 T+1/T+5 胜率与官方基准对照', async () => {
    mocks.hitRate.mockResolvedValue({
      days: 30,
      by_type: {
        resonance: {
          t1: { n: 40, win_rate: 52.5, avg_pct: 1.2 },
          t5: { n: 25, win_rate: 60.0, avg_pct: 2.4 },
          official_benchmark: { win_rate: 75.42, pl_ratio: 3.45, note: '官方口径' },
        },
      },
    })
    render(<DecisionLedger />)
    const card = await screen.findByTestId('signal-reconcile')
    expect(card.textContent).toContain('三指标共振')
    expect(card.textContent).toContain('52.50%')
    expect(card.textContent).toContain('60.00%')
    // 官方基准对照上屏(口径不完全一致的提醒在 title)
    expect(card.textContent).toContain('75.42%')
  })

  it('胜率为 null(样本不足)显示 `--`, 不显示 0%', async () => {
    mocks.hitRate.mockResolvedValue({
      days: 30,
      by_type: { gs_signal: { t1: { n: 3, win_rate: null, avg_pct: null }, t5: { n: 0, win_rate: null, avg_pct: null } } },
    })
    render(<DecisionLedger />)
    const card = await screen.findByTestId('signal-reconcile')
    expect(card.textContent).toContain('GS 信号')
    expect(card.textContent).not.toContain('0.00%')
  })
})

describe('DecisionLedger · 入场候选后验卡片(P2-3)', () => {
  it('样本不足行显式 `--` + insufficient 提示', async () => {
    mocks.entryOutcomes.mockResolvedValue({
      ...emptyOutcomes,
      rows: [
        { horizon_days: 5, source: 'watchlist', source_label: '自选', total: 3, wins: 3, win_rate: null, avg_return_pct: null, insufficient: true, min_sample: 20 },
      ],
      total_samples: 3,
    })
    render(<DecisionLedger />)
    const card = await screen.findByTestId('entry-outcomes')
    expect(card.textContent).toContain('T+5')
    expect(card.textContent).toContain('--')
    expect(card.textContent).not.toContain('100.00%') // 3/3 不能算成 100%
  })

  it('有样本行渲染胜率与均收益', async () => {
    mocks.entryOutcomes.mockResolvedValue({
      ...emptyOutcomes,
      rows: [
        { horizon_days: 3, source: 'market_scan', source_label: '市场扫描', total: 50, wins: 30, win_rate: 60.0, avg_return_pct: 2.5, insufficient: false, min_sample: 20 },
      ],
    })
    render(<DecisionLedger />)
    const card = await screen.findByTestId('entry-outcomes')
    expect(card.textContent).toContain('市场扫描')
    expect(card.textContent).toContain('60.00%')
  })

  it('查询失败态显式(available=false), 不编造数字', async () => {
    mocks.entryOutcomes.mockResolvedValue({ ...emptyOutcomes, available: false, error: 'db down', rows: [] })
    render(<DecisionLedger />)
    const card = await screen.findByTestId('entry-outcomes')
    expect(card.textContent).toContain('查询失败')
    expect(card.textContent).toContain('db down')
  })
})

describe('DecisionLedger · 共振回测入口(P2-1)', () => {
  it('点回测 → 结果带 basis 口径标记透传上屏', async () => {
    mocks.backtest.mockResolvedValue({
      available: true,
      params: { fund_source: 'ohlc' },
      basis: '三指标(资金=OHLC对照项)',
      sample: { symbols: 2, signals: 7 },
      by_phase: { 向好: { count: 7, win_rate: 0.7, profit_ratio: 3.2, avg_gain: 5, avg_loss: 1.5, path_max_gain_avg: null, path_max_loss_avg: null } },
      official: { 向好: { win_rate: 75.42, pl_ratio: 3.45 } },
      generated_at: '2026-10-10T10:00:00',
      note: '对照项误差大',
    })
    render(<DecisionLedger />)
    fireEvent.click(await screen.findByText('回测'))
    const badge = await screen.findByTestId('backtest-basis')
    expect(badge.textContent).toContain('三指标(资金=OHLC对照项)')
    // 官方基准作为对照一并上屏
    expect(screen.getByTestId('backtest-result').textContent).toContain('75.42')
  })

  it('缺股票池/失败 → available=false 显式, 不出结果表', async () => {
    mocks.backtest.mockResolvedValue({
      available: false,
      error: '未提供股票池(symbols 为空) —— 无样本可回测',
      params: {},
      basis: '双指标(缺资金维)',
      sample: { symbols: 0, signals: 0 },
      note: '',
    })
    render(<DecisionLedger />)
    fireEvent.click(await screen.findByText('回测'))
    const degraded = await screen.findByTestId('backtest-unavailable')
    expect(degraded.textContent).toContain('股票池')
    expect(screen.queryByTestId('backtest-result')).toBeNull()
  })
})

describe('DecisionLedger · 账本明细分页(P2-4)', () => {
  it('下一页 → 以 offset=20 重新取数', async () => {
    mocks.log.mockResolvedValueOnce({
      count: 20,
      total: 100,
      offset: 0,
      limit: 20,
      has_more: true,
      items: [
        { signal_kind: 'resonance3', symbol: '600000', trade_date: '20260901', price_at_signal: 10, context: '{}', source: '', outcomes: { t1: { ret: null, hit: null }, t3: { ret: null, hit: null }, t5: { ret: null, hit: null } }, filled_at: null },
      ],
      note: '',
    })
    mocks.log.mockResolvedValue({
      count: 20,
      total: 100,
      offset: 20,
      limit: 20,
      has_more: true,
      items: [],
      note: '',
    })
    render(<DecisionLedger />)
    await waitFor(() => expect(mocks.log).toHaveBeenCalledTimes(1))
    expect(mocks.log).toHaveBeenCalledWith(undefined, 20, 0, undefined, undefined)
    fireEvent.click(await screen.findByText('下一页'))
    await waitFor(() => expect(mocks.log).toHaveBeenCalledWith(undefined, 20, 20, undefined, undefined))
  })

  it('末页(has_more=false)下一页禁用', async () => {
    mocks.log.mockResolvedValue({ ...emptyLog, total: 5, has_more: false })
    render(<DecisionLedger />)
    const next = await screen.findByText('下一页')
    await waitFor(() => expect((next as HTMLButtonElement).disabled).toBe(true))
  })
})
