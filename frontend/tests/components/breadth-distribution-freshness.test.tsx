// @vitest-environment jsdom
//
// 涨跌分布(BreadthDistributionChart)的「后台计算可区分 / 可重试 / 数据时效不臆测」契约。
//
// 后端 `/market-data/breadth-distribution` 异步化后, 缓存未命中立即返回 `{pending:true}`,
// 后台线程要算数十秒。此前前端把「计算中」与「算超时」混成一句话, 失败态只有「稍后自动重试」
// 却没有任何重试入口, 拿到真快照也不标数据时间 —— 用户无法判断画布上的分布是刚算的还是陈的。
//
// 本测试钉住:
//   1. pending 长时间 → 显式「计算超时」+ 可重试(不无限假装在算);
//   2. 失败 → 显式失败 + 重试按钮, 点重试恢复真实分布;
//   3. 真实快照带 captured_at/age_s → 显示数据时间与相对时效; 缺字段 → 「数据时效未知」;
//   4. stale 快照 → 明示可能已过期(不冒充新鲜)。
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn(), setOption: vi.fn() }))

vi.mock('@sida/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('@sida/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, on: vi.fn(), off: vi.fn(), resize: vi.fn() } },
  }),
}))
vi.mock('@sida/biz-ui/lib/echarts-core', () => ({
  default: { graphic: { LinearGradient: class {} } },
}))
vi.mock('@sida/biz-ui/lib/stock-colors', () => ({
  readStockColors: () => ({ up: '#ef4444', down: '#22c55e' }),
  withAlpha: (c: string) => c,
}))

import BreadthDistributionChart, { formatBreadthFreshness } from '@sida/biz-ui/components/dashboard/BreadthDistributionChart'

const BUCKETS = ['涨停', '>5%', '3~5%', '1~3%', '-1~1%', '-3~-1%', '-5~-3%', '<-5%', '跌停']
const items = (n: number) => BUCKETS.map((bucket) => ({ bucket, count: n }))
const PENDING = { count: 0, total: 0, items: items(0), note: '后台计算中', pending: true }
const READY = {
  count: 4200,
  total: 5200,
  items: items(0).map((b, i) => ({ ...b, count: 100 * (i + 1) })),
  note: '',
  pending: false,
}

beforeEach(() => {
  vi.useFakeTimers()
  mocks.fetchAPI.mockReset()
  mocks.setOption.mockReset()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('BreadthDistributionChart 计算超时/失败/CAPTURED', () => {
  it('后台长期 pending → 区分「计算中」与「计算超时」, 超时给重试入口', async () => {
    mocks.fetchAPI.mockResolvedValue(PENDING)
    render(<BreadthDistributionChart />)
    await flush()

    expect(screen.getByText(/涨跌分布后台计算/)).toBeTruthy()
    expect(screen.queryByText(/超时/)).toBeNull()

    // 跨过后台计算上限(≈60s) —— 不得继续假装在算
    await act(async () => {
      await vi.advanceTimersByTimeAsync(61_000)
    })
    expect(screen.getByText(/涨跌分布后台计算超时/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()
  })

  it('超时后点重试 → 重新拉取, 算完立即画真实分布', async () => {
    mocks.fetchAPI.mockResolvedValue(PENDING)
    render(<BreadthDistributionChart />)
    await flush()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(61_000)
    })

    mocks.fetchAPI.mockResolvedValue({ ...READY, captured_at: '2026-11-07T14:30:00', age_s: 45 })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '重试' }))
    })
    await flush()

    expect(mocks.setOption).toHaveBeenCalled()
    expect(screen.queryByText(/超时/)).toBeNull()
    expect(screen.getByText(/数据时间/)).toBeTruthy()
  })

  it('失败 → 显式失败文案, 点重试可恢复(失败≠无数据)', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('boom'))
    render(<BreadthDistributionChart />)
    await flush()

    expect(screen.getByText(/涨跌分布加载失败/)).toBeTruthy()
    expect(screen.getByText(/boom/)).toBeTruthy()
    expect(mocks.setOption).not.toHaveBeenCalled()

    mocks.fetchAPI.mockResolvedValue({ ...READY, captured_at: '2026-11-07T14:30:00', age_s: 30 })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '重试' }))
    })
    await flush()

    expect(screen.queryByText(/涨跌分布加载失败/)).toBeNull()
    expect(mocks.setOption).toHaveBeenCalled()
  })

  it('真实快照带 captured_at/age_s → 标注数据时间 + 相对时效', async () => {
    mocks.fetchAPI.mockResolvedValue({ ...READY, captured_at: '2026-11-07T14:30:00', age_s: 90 })
    render(<BreadthDistributionChart />)
    await flush()

    expect(screen.getByText(/数据时间 11-07 14:30/)).toBeTruthy()
    expect(screen.getByText(/1分钟前/)).toBeTruthy()
    expect(screen.queryByText(/时效未知/)).toBeNull()
  })

  it('快照缺 captured_at/age_s → 写「数据时效未知」, 不拿当前时间臆测', async () => {
    mocks.fetchAPI.mockResolvedValue(READY)
    render(<BreadthDistributionChart />)
    await flush()

    expect(screen.getByText(/数据时效未知/)).toBeTruthy()
    expect(screen.queryByText(/数据时间/)).toBeNull()
  })

  it('stale 快照 → 明示可能已过期', async () => {
    mocks.fetchAPI.mockResolvedValue({
      ...READY,
      captured_at: '2026-11-07T09:00:00',
      age_s: 21_600,
      stale: true,
    })
    render(<BreadthDistributionChart />)
    await flush()

    expect(screen.getByText(/6小时前/)).toBeTruthy()
    expect(screen.getByText(/可能已过期/)).toBeTruthy()
  })

  it('formatBreadthFreshness: 只有 age_s 时按相对时效, 不编造时刻', () => {
    expect(formatBreadthFreshness({ age_s: 12_345 })).toBe('数据更新于 3小时前')
    expect(formatBreadthFreshness({})).toBe('数据时效未知')
    expect(formatBreadthFreshness({ captured_at: '不是时间' })).toBe('数据时效未知')
  })
})
