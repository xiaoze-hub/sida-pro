// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
// lightweight-charts 需要 canvas，jsdom 跑不了 → 只验证数据/状态，不验证绘图
vi.mock('@sida/biz-ui/components/MinuteLwcChart', () => ({
  default: ({ points, height }: { points: unknown[]; height?: number }) => (
    <div data-testid="lwc-stub" data-points={points.length} data-height={height} />
  ),
}))

import IndexTrendBand, { computeFacts } from '@/components/dashboard/IndexTrendBand'
import type { MinutePoint } from '@sida/biz-ui/components/MinuteLwcChart'

const pt = (t: string, price: number, volume: number): MinutePoint => ({ t, price, avg: price, volume })

/** 上午 121 点(09:30–11:30), 下午 121 点(13:00–15:00) */
function series(): MinutePoint[] {
  const out: MinutePoint[] = []
  for (let i = 0; i < 121; i++) {
    const hh = 9 + Math.floor((30 + i) / 60)
    const mm = (30 + i) % 60
    out.push(pt(`${String(hh).padStart(2, '0')}${String(mm).padStart(2, '0')}`, 3600 + i, 1000))
  }
  for (let i = 0; i < 121; i++) {
    const hh = 13 + Math.floor(i / 60)
    const mm = i % 60
    out.push(pt(`${String(hh).padStart(2, '0')}${String(mm).padStart(2, '0')}`, 3721 + i, 800))
  }
  return out
}

const RESP = {
  points: series(),
  prev_close: 3600,
  is_index: true,
  degraded: false,
  note: null,
  trade_date: '20261023',
}

describe('computeFacts 三个事实（纯函数）', () => {
  it('日内位置 = (现价-今低)/(今高-今低)：末点即最高 → 100%', () => {
    const f = computeFacts(series())
    expect(Math.round(f.position ?? -1)).toBe(100)
  })

  it('日内位置能区分「收在低位」—— 高位收盘不能一律 100', () => {
    const pts = series()
    pts[pts.length - 1] = pt('1500', 3610, 800) // 从 3841 砸回来
    const f = computeFacts(pts)
    expect(f.position).not.toBeNull()
    expect(f.position!).toBeLessThan(5)
  })

  it('尾盘30分：以 14:30 为基准算涨跌', () => {
    const f = computeFacts(series())
    // 14:30 落在下午第 91 点附近(13:00 起算), 末点更高 → 正数
    expect(f.tail30).not.toBeNull()
    expect(f.tail30!).toBeGreaterThan(0)
  })

  it('午后/上午量比：上午 1000/点 ×121、下午 800/点 ×121 → ≈0.80', () => {
    const f = computeFacts(series())
    expect(f.pmAmRatio).toBeCloseTo(0.8, 2)
  })

  it('空数据不编造：三项全部 null，不是 0', () => {
    const f = computeFacts([])
    expect(f).toEqual({ position: null, positionRange: null, tail30: null, pmAmRatio: null })
  })

  it('全天只有一条平线时位置为 null（振幅 0 不该显示 0% 或 100%）', () => {
    const pts = [pt('0930', 3600, 100), pt('0931', 3600, 100)]
    expect(computeFacts(pts).position).toBeNull()
  })
})

describe('IndexTrendBand 四种诚实态', () => {
  afterEach(() => cleanup())
  // 用 mockClear 不用 mockReset: 本文件里 mockReset 之后任何抛错都会被 vitest
  // 记成 unhandled 直接判失败(踩过), mockClear 足够清调用记录。
  beforeEach(() => mocks.fetchAPI.mockClear())

  it('正常：画分时 + 三个事实 + 基准日取自接口而非今天', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<IndexTrendBand />)
    await waitFor(() => expect(screen.getByTestId('index-trend-band')).toBeTruthy())
    expect(screen.getByTestId('trend-tab-000001').getAttribute('aria-selected')).toBe('true')
    expect(await screen.findByTestId('lwc-stub')).toBeTruthy()
    // 今天 2026-10-24 是周六, 数据是 10-23 —— 必须显示基准日, 不能显示成今天
    expect(screen.getByText('基准日 2026-10-23')).toBeTruthy()
    expect(screen.getByTestId('fact-position')).toBeTruthy()
    expect(screen.getByTestId('fact-tail30')).toBeTruthy()
    expect(screen.getByTestId('fact-pm-am')).toBeTruthy()
  })

  it('源故障(degraded)：显式说源异常，不画线', async () => {
    mocks.fetchAPI.mockResolvedValue({ ...RESP, points: [], degraded: true, note: '腾讯超时' })
    render(<IndexTrendBand />)
    const el = await screen.findByTestId('trend-degraded')
    expect(el.textContent).toContain('腾讯超时')
    expect(el.textContent).toContain('不等于停牌')
    expect(screen.queryByTestId('lwc-stub')).toBeNull()
  })

  it('真空态(非交易日)：说"当日无分时"，不冒充故障', async () => {
    mocks.fetchAPI.mockResolvedValue({ ...RESP, points: [], degraded: false })
    render(<IndexTrendBand />)
    expect((await screen.findByTestId('trend-empty')).textContent).toContain('非交易日')
    expect(screen.queryByTestId('lwc-stub')).toBeNull()
  })

  it('请求失败：给错误原文 + 重试入口，不显示 "--" 装作正常', async () => {
    // 必须用 mockRejectedValueOnce: 持久的 mockRejectedValue/mockImplementation 会造出
    // 多个已 rejected 的 promise(组件在 effect 里可能被调用两次), 未被及时接住的那个
    // 会被 vitest 记成 unhandled 并直接判本用例失败。
    mocks.fetchAPI.mockRejectedValueOnce(new Error('502'))
    render(<IndexTrendBand />)
    const el = await screen.findByTestId('trend-error')
    expect(el.textContent).toContain('502')
    expect(el.textContent).toContain('重试')
    expect(screen.queryByTestId('lwc-stub')).toBeNull()
  })

  it('缺 trade_date 时写"最近交易日"，不猜今天', async () => {
    mocks.fetchAPI.mockResolvedValue({ ...RESP, trade_date: null })
    render(<IndexTrendBand />)
    await screen.findByTestId('lwc-stub')
    expect(screen.getByText('最近交易日')).toBeTruthy()
  })
})
