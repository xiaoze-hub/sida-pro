// @vitest-environment jsdom
//
// 涨跌分布(BreadthDistributionChart)的「时效会走表 / 回补不叠请求 / 超时说法即行为 / 旧图可手动重试」契约。
//
// 上一轮(797edc61)把「计算中/超时/失败」区分开了, 但留下四处回归, 本文件逐条钉住:
//   1. `age_s` 是后端**响应那一刻**算的。拿它当永久真值 → 5 分钟前的快照永远写「5分钟前」,
//      越挂越像刚采的。时效必须随「收到响应的本地时刻」往前走(不拿前端时钟猜采集时刻)。
//   2. 回补每 1.5s 硬发一次, 慢响应(>1.5s)会被后发的请求顶掉序号, 算完的结果反被当过期丢弃。
//      必须单飞: 在途那笔没回来就不发下一笔。
//   3. 文案写「不再自动补齐」, 但 60s 轮询照旧在请求 —— 说法与行为不一致。到点即停(回补+自动刷新),
//      手动重试后才恢复。
//   4. 已有旧图时刷新失败只留一行字, 没有手动重试入口。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  // 默认: 后端后台仍在算 —— 让「未显式排队的调用」不会把已有数据覆盖成空分布
  mocks.fetchAPI.mockResolvedValue(PENDING)
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

/** 冲掉一次已 resolve 的取数微任务 */
async function flush() {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('BreadthDistributionChart 时效/回补/超时行为', () => {
  it('回补遇到慢响应(>1.5s)时不再叠加请求, 慢结果也不会被序号顶掉', async () => {
    mocks.fetchAPI.mockResolvedValueOnce(PENDING) // 挂载
    let release!: (v: unknown) => void
    const slow = new Promise((res) => {
      release = res
    })
    mocks.fetchAPI.mockImplementationOnce(() => slow) // 第一次回补: 慢

    render(<BreadthDistributionChart />)
    await flush()

    // 旧实现每 1.5s 硬发一次, 6s 内会叠到 5 笔(挂载 + 4 次回补)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000)
    })
    expect(mocks.fetchAPI).toHaveBeenCalledTimes(2)

    // 在途那笔算完了: 它是**最新**一次请求, 结果必须落地, 不能被判成过期响应
    await act(async () => {
      release({ ...READY, captured_at: '2026-11-07T14:30:00', age_s: 20 })
      await Promise.resolve()
    })
    expect(mocks.setOption).toHaveBeenCalled()
    expect(screen.queryByText(/涨跌分布后台计算中/)).toBeNull()
  })

  it('真实定时器: 慢回补(3.4s)期间不发新请求, 算完即画', async () => {
    vi.useRealTimers()
    mocks.fetchAPI.mockResolvedValueOnce(PENDING) // 挂载
    let release!: (v: unknown) => void
    const slow = new Promise((res) => {
      release = res
    })
    mocks.fetchAPI.mockImplementationOnce(() => slow) // t≈1.5s 的第一次回补: 慢

    render(<BreadthDistributionChart />)
    await waitFor(() => expect(mocks.fetchAPI).toHaveBeenCalledTimes(1))
    // t≈3.6s: 旧实现会在 t=3.0s 再发一笔(立刻拿回 pending)把慢响应的序号顶掉
    await act(async () => {
      await new Promise((r) => setTimeout(r, 3600))
    })
    expect(mocks.fetchAPI).toHaveBeenCalledTimes(2)

    await act(async () => {
      release({ ...READY, captured_at: '2026-11-07T14:30:00', age_s: 5 })
      await Promise.resolve()
    })
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    expect(screen.queryByText(/涨跌分布后台计算中/)).toBeNull()
  })

  it('计算超时后, 文案与行为一致: 回补与 60s 自动刷新都停', async () => {
    render(<BreadthDistributionChart />)
    await flush()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(61_000)
    })
    expect(screen.getByText(/涨跌分布后台计算超时/)).toBeTruthy()
    expect(screen.getByText(/已停止自动重试/)).toBeTruthy()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()

    const calls = mocks.fetchAPI.mock.calls.length
    // 再挂 5 分钟: 回补不应继续, 60s 常规刷新也不应继续
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000)
    })
    expect(mocks.fetchAPI.mock.calls.length).toBe(calls)
  })

  it('超时后手动重试 → 恢复取数并重新计时(60s 自动刷新同样恢复)', async () => {
    render(<BreadthDistributionChart />)
    await flush()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(61_000)
    })
    expect(screen.getByText(/涨跌分布后台计算超时/)).toBeTruthy()

    mocks.fetchAPI.mockResolvedValue({ ...READY, captured_at: '2026-11-07T14:30:00', age_s: 10 })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '重试' }))
    })
    await flush()

    expect(mocks.setOption).toHaveBeenCalled()
    expect(screen.queryByText(/涨跌分布后台计算超时/)).toBeNull()

    const calls = mocks.fetchAPI.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_500)
    })
    expect(mocks.fetchAPI.mock.calls.length).toBeGreaterThan(calls)
  })

  it('age_s 不冻结: 挂 5 分钟后同一快照的时效必须往前走', async () => {
    mocks.fetchAPI.mockResolvedValueOnce({ ...READY, captured_at: '2026-11-07T14:30:00', age_s: 30 })
    render(<BreadthDistributionChart />)
    await flush()

    expect(screen.getByText(/30秒前/)).toBeTruthy()
    expect(screen.getByText(/数据时间 11-07 14:30/)).toBeTruthy()

    // 期间刷新仍返回 pending(后台重算), meta 不变; 时效只能靠本地走表推进
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300_000)
    })
    expect(screen.getByText(/5分钟前/)).toBeTruthy()
    expect(screen.queryByText(/30秒前/)).toBeNull()
  })

  it('已有旧图时刷新失败 → 保留旧图与真实时效, 并给手动重试入口', async () => {
    mocks.fetchAPI.mockResolvedValueOnce({ ...READY, captured_at: '2026-11-07T14:30:00', age_s: 60 })
    render(<BreadthDistributionChart />)
    await flush()
    expect(mocks.setOption).toHaveBeenCalled()

    mocks.fetchAPI.mockRejectedValueOnce(new Error('网络断了'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(screen.getByText(/最近一次刷新失败/)).toBeTruthy()
    expect(screen.getByText(/网络断了/)).toBeTruthy()
    expect(screen.getByText(/数据时间 11-07 14:30/)).toBeTruthy()
    // 旧图没被清空(画布容器还在, 也没被换成空分布文案), 且失败行给了手动重试入口
    expect(document.querySelector('.h-\\[160px\\].w-full')).toBeTruthy()
    expect(screen.queryByText(/暂无分布数据/)).toBeNull()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()

    // 失败不冻结时效: 收到快照时 60s 旧, 再过 180s 就是 4分钟前
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(screen.getByText(/4分钟前/)).toBeTruthy()

    mocks.fetchAPI.mockResolvedValueOnce({ ...READY, captured_at: '2026-11-07T14:35:00', age_s: 5 })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '重试' }))
    })
    await flush()

    expect(screen.queryByText(/最近一次刷新失败/)).toBeNull()
    expect(screen.getByText(/数据时间 11-07 14:35/)).toBeTruthy()
    expect(screen.getByText(/5秒前/)).toBeTruthy()
  })

  it('formatBreadthFreshness: age_s 以收到响应的时刻为基准推进, 缺字段不臆测', () => {
    const received = 1_000_000
    expect(formatBreadthFreshness({ age_s: 30, received_at: received }, received)).toBe('数据更新于 30秒前')
    expect(formatBreadthFreshness({ age_s: 30, received_at: received }, received + 60_000)).toBe('数据更新于 1分钟前')
    expect(formatBreadthFreshness({ age_s: 30, received_at: received }, received + 300_000)).toBe('数据更新于 5分钟前')

    // 有采集时刻 + age_s: 时刻来自后端, 相对时效同样往前走
    const meta = { captured_at: '2026-11-07T14:30:00', age_s: 90, received_at: received }
    expect(formatBreadthFreshness(meta, received)).toBe('数据时间 11-07 14:30 · 1分钟前')
    expect(formatBreadthFreshness(meta, received + 90_000)).toBe('数据时间 11-07 14:30 · 3分钟前')

    // 无 received_at 的历史调用: 保持静态口径(不假装知道它有多旧)
    expect(formatBreadthFreshness({ age_s: 12_345 })).toBe('数据更新于 3小时前')
    expect(formatBreadthFreshness({})).toBe('数据时效未知')
    expect(formatBreadthFreshness({ captured_at: '不是时间' })).toBe('数据时效未知')
    // stale 仍只采信后端判断
    expect(formatBreadthFreshness({ age_s: 30, received_at: received, stale: true }, received)).toBe(
      '数据更新于 30秒前 · 可能已过期',
    )
  })
})
