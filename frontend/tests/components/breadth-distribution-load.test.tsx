// @vitest-environment jsdom
//
// perf(2026-10-02) 回归钉: 涨跌幅分布(首页「涨跌分布」canvas)的**先出壳后补数**加载链。
//
// 背景: 后端 `/market-data/breadth-distribution` 自 2026-09-18 起异步化 —— 缓存未命中立即返回
// `{pending:true, 全 0}` 并起后台线程算(数秒~数十秒)。而组件此前只在 **60s 定时器**上重拉:
// 后台几秒就算完的分布, 首屏画布要等下一个 60s tick 才出现(B1 走查「涨跌分布迟迟不 settle」)。
//
// 本测试全 mock(禁真实网络), 钉住四条契约:
//   1. pending 时先出壳(占满同高度 + 显式文案), 绝不把 0 渲染成分布柱;
//   2. 命中 pending 后以短间隔回补轮询, 算完**立即**画(不必等 60s);
//   3. 拉取失败且从未拿到数据 → 显式失败态(与\"暂无分布数据\"区分), 不画假柱;
//   4. 竞态守卫: 早发晚到的过期响应被丢弃, 不覆盖新结果。
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn(), setOption: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('@panwatch/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, on: vi.fn(), off: vi.fn(), resize: vi.fn() } },
  }),
}))
vi.mock('@panwatch/biz-ui/lib/echarts-core', () => ({
  default: { graphic: { LinearGradient: class {} } },
}))
vi.mock('@panwatch/biz-ui/lib/stock-colors', () => ({
  readStockColors: () => ({ up: '#ef4444', down: '#22c55e' }),
  withAlpha: (c: string) => c,
}))

import BreadthDistributionChart from '@panwatch/biz-ui/components/dashboard/BreadthDistributionChart'

const BUCKETS = ['涨停', '>5%', '3~5%', '1~3%', '-1~1%', '-3~-1%', '-5~-3%', '<-5%', '跌停']
function items(n: number) {
  return BUCKETS.map((bucket) => ({ bucket, count: n }))
}
const PENDING = { count: 0, total: 0, items: items(0), note: '后台计算中', pending: true }
const READY = { count: 4200, total: 5200, items: items(0).map((b, i) => ({ ...b, count: 100 * (i + 1) })), note: '', pending: false }

beforeEach(() => {
  vi.useFakeTimers()
  mocks.fetchAPI.mockReset()
  mocks.setOption.mockReset()
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

describe('BreadthDistributionChart 异步分布加载链(先出壳后补数)', () => {
  it('pending → 先出壳(显式文案, 不画 0 柱); 回补轮询算完立即画(不必等 60s)', async () => {
    mocks.fetchAPI.mockResolvedValueOnce(PENDING)
    render(<BreadthDistributionChart />)
    await flush()

    // 先出壳: 显式“计算中”, 且没有 setOption(不用 0 假装有分布)
    expect(screen.getByText(/涨跌分布后台计算中/)).toBeTruthy()
    expect(mocks.setOption).not.toHaveBeenCalled()

    // 后台算完: 下一次回补(1.5s 间隔)拿到 ready
    mocks.fetchAPI.mockResolvedValue(READY)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600)
    })

    // 关键: 只推进 1.6s 画布就出数据了 —— 旧实现要等下一个 60s tick
    expect(mocks.setOption).toHaveBeenCalled()
    expect(screen.queryByText(/涨跌分布后台计算中/)).toBeNull()
    // 且未推进到 60s 常规刷新
    expect(mocks.fetchAPI.mock.calls.length).toBeLessThan(3)
  })

  it('常规刷新仍保持原节奏: 无 pending 时不额外轮询', async () => {
    mocks.fetchAPI.mockResolvedValue(READY)
    render(<BreadthDistributionChart />)
    await flush()
    expect(mocks.fetchAPI).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    // 10s 内无回补(未命中 pending), 只有挂载时那一次
    expect(mocks.fetchAPI).toHaveBeenCalledTimes(1)
  })

  it('拉取失败且从未拿到数据 → 显式失败态, 不渲染 0 柱', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('boom'))
    render(<BreadthDistributionChart />)
    await flush()

    expect(screen.getByText(/涨跌分布加载失败/)).toBeTruthy()
    expect(mocks.setOption).not.toHaveBeenCalled()
  })

  it('就绪但全 0 → 透传后端 note(或"暂无分布数据"), 不画 0 柱', async () => {
    mocks.fetchAPI.mockResolvedValue({ count: 0, total: 0, items: items(0), note: '数据源返回为空(可能非交易日)', pending: false })
    const { container } = render(<BreadthDistributionChart />)
    await flush()

    expect(screen.getByText(/数据源返回为空/)).toBeTruthy()
    // 空态分支不挂画布容器(全 0 不会被画成一根根 0 柱)
    expect(container.querySelector('.w-full')).toBeNull()
  })

  it('竞态守卫: 早发晚到的过期响应被丢弃, 不覆盖已画出的新结果', async () => {
    // 挂载: pending(先出壳, 建立回补轮询)
    mocks.fetchAPI.mockResolvedValueOnce(PENDING)
    // 回补第 1 次(t=1.5s): 慢响应, 手动放行
    let releaseSlow!: (v: unknown) => void
    const slow = new Promise((res) => {
      releaseSlow = res
    })
    mocks.fetchAPI.mockImplementationOnce(() => slow)
    // 回补第 2 次(t=3s): 先返回真实数据
    mocks.fetchAPI.mockResolvedValueOnce(READY)

    render(<BreadthDistributionChart />)
    await flush()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600) // 触发慢响应(t=1.5s, 仍未回)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600) // 触发 ready(t=3s, 已回)
    })
    expect(mocks.setOption).toHaveBeenCalled()

    // 现在放行那条**更早发起**的慢响应 —— 它已过期(seq 落后), 必须被丢弃
    await act(async () => {
      releaseSlow({ count: 0, total: 0, items: items(0), note: '后台计算中', pending: true })
      await Promise.resolve()
    })
    expect(screen.queryByText(/涨跌分布后台计算中/)).toBeNull()
    expect(screen.queryByText(/暂无分布数据/)).toBeNull()
  })
})
