// @vitest-environment jsdom
//
// 加载韧性(2026-10-10): DecisionLedger 首屏加载门控 + 失败态重试。
//
// 背景: 核心两段(stats/log)取不到时, 页面此前会**渲染一堆空表**并把失败读成「暂无数据」;
// 且首屏没有显式加载提示。修复: 加载中显式 / 硬失败显式错误态 + 「重试」。
//
// 本测试禁真实网络(mock 网络层 decisionsApi/signalsReviewApi), 组件走真实代码:
//   1. 首屏取数未回 → 显式「加载中…」, 不出空表冒充无数据;
//   2. 核心取数失败 → 显式错误态(role=alert) + 「重试」按钮, 不渲染空表;
//   3. 点「重试」→ 重新取数成功 → 正常渲染, 错误态消失。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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
  signalsReviewApi: { hitRate: (...a: unknown[]) => mocks.hitRate(...a) },
  kindLabel: (k: string) => k,
}))

import DecisionLedger from '@/pages/DecisionLedger'

const emptyStats = { since: '2026-04-01', min_sample: 30, rows: [], note: '' }
const emptyHitRate = { days: 30, by_type: {} }
const emptyOutcomes = { window_days: 30, min_sample: 20, available: true, rows: [], note: '' }
const emptyLog = { count: 0, total: 0, offset: 0, limit: 20, has_more: false, items: [], note: '' }

beforeEach(() => {
  vi.clearAllMocks()
  mocks.stats.mockResolvedValue(emptyStats)
  mocks.log.mockResolvedValue(emptyLog)
  mocks.hitRate.mockResolvedValue(emptyHitRate)
  mocks.entryOutcomes.mockResolvedValue(emptyOutcomes)
  mocks.thresholds.mockResolvedValue({ items: [], note: '' })
})

afterEach(() => cleanup())

describe('DecisionLedger 首屏门控 + 失败态重试(加载韧性)', () => {
  it('首屏取数未回 → 显式「加载中…」, 不出空表冒充无数据', async () => {
    let releaseStats!: (v: unknown) => void
    mocks.stats.mockReturnValueOnce(
      new Promise((res) => {
        releaseStats = res
      }),
    )
    render(<DecisionLedger />)

    expect(screen.getByText('加载中…')).toBeTruthy()
    // 关键: 加载中不得渲染「还没有留痕信号」这类空态(会被读成"没数据")
    expect(screen.queryByText(/还没有留痕信号/)).toBeNull()

    await act(async () => {
      releaseStats(emptyStats)
    })
  })

  it('核心取数失败 → 显式错误态 + 「重试」, 不渲染空表', async () => {
    mocks.stats.mockRejectedValueOnce(
      Object.assign(new Error('502 Bad Gateway'), { kind: 'HTTP_5xx', status: 502 }),
    )
    render(<DecisionLedger />)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('服务暂时不可用') // HTTP_5xx → ErrorState.server
    expect(screen.getByText('重试')).toBeTruthy()
    expect(screen.queryByText(/还没有留痕信号/)).toBeNull()
    expect(screen.queryByText('加载中…')).toBeNull()
  })

  it('点「重试」重新取数成功 → 正常渲染, 错误态消失', async () => {
    mocks.stats.mockRejectedValueOnce(new Error('请求超时，请稍后重试'))
    render(<DecisionLedger />)
    await screen.findByRole('alert')

    await act(async () => {
      fireEvent.click(screen.getByText('重试'))
    })
    await waitFor(() => expect(screen.getByTestId('thresholds')).toBeTruthy())
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByTestId('signal-reconcile')).toBeTruthy()
  })
})
