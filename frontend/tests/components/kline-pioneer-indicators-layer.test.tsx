// @vitest-environment jsdom
//
// 决策先锋辅助指标 K 线图层(2026-10-10 P3 UI 集成)渲染回归: 真渲染 KlineChart(canvas 层换成假图表库,
// 见 vite.config test.alias), 只 mock 取数。钉六件事:
//  ① 自取: 组件向 `/indicators/trend-line/{symbol}` 与 `/indicators/niuxiong/{symbol}` 取三线(父不传时);
//  ② 图例分组共存: `trend-line-legend` / `niuxiong-legend` / `pattern-legend` 同时在, 互不吃掉;
//  ③ 门控: `layersVisible.{trendLine,niuxiong}=false` → 不发对应请求, 也不渲染对应图例;
//  ④ 父接管: 传了 `trendLine` prop → 不再自取 trend-line;
//  ⑤ 分时突破降级: `available=false` → 提示条显式「逐分钟 DDE 未接入 · 暂不出信号」(不编造信号);
//  ⑥ 分时突破信号: `available=true, signal_type='突'` → 提示条出「突」+ 触发条件;
//  另: 图例不含买卖建议字样(信号是证据非建议)。
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

const H = vi.hoisted(() => ({
  calls: [] as string[],
  trendLine: {} as Record<string, unknown>,
  niuxiong: {} as Record<string, unknown>,
  minute: {} as Record<string, unknown>,
  patterns: [] as Array<Record<string, unknown>>,
}))

vi.mock('@panwatch/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string) => {
    const u = String(url)
    H.calls.push(u)
    if (u.includes('/indicators/trend-line/')) return Promise.resolve(H.trendLine)
    if (u.includes('/indicators/niuxiong/')) return Promise.resolve(H.niuxiong)
    if (u.includes('/indicators/minute-breakthrough/')) return Promise.resolve(H.minute)
    if (u.includes('/patterns')) return Promise.resolve({ symbol: '002636', patterns: H.patterns })
    if (u.includes('/summary')) return Promise.resolve({})
    return Promise.resolve({
      klines: [
        { date: '2026-03-05', open: 10, high: 10.5, low: 9.5, close: 10.2, volume: 1000 },
        { date: '2026-03-06', open: 10.2, high: 10.8, low: 10.0, close: 10.5, volume: 1100 },
        { date: '2026-03-07', open: 10.5, high: 11.2, low: 10.4, close: 11.0, volume: 1300 },
      ],
    })
  }),
}))

import KlineChart from '@panwatch/biz-ui/components/KlineChart'

beforeEach(() => {
  H.calls.length = 0
  H.trendLine = {
    available: true,
    lines: { red: 11, yellow: 10.5, green: 9.8 },
    band: { state: '多方带', low: 10.5, high: 11 },
    trend: '升势',
    bar_time: '2026-03-07',
    buy_points: [{ signal: 'buy', rule: 'pullback_green_yang', trigger: '回踩绿线收阳', time: '2026-03-07' }],
    sell_points: [],
    params: { red_period: 3, yellow_period: 3, green_period: 3 },
    calibration: '逆向近似待校准(EMA 近似, 参数见 thresholds 配置层)',
  }
  H.niuxiong = {
    available: true,
    lines: { bull: 11, horse: 10, trade: 9 },
    signal: 'B',
    color: 'red',
    state: '牛线上方',
    cross: { type: 'golden', direction: 'B', bars_ago: 1, time: '2026-03-07' },
    params: { bull_period: 3, horse_period: 2, trade_period: 3 },
    calibration: '买卖线口径逆向近似待校准',
  }
  H.minute = { available: false, degraded: true, reasons: ['DDE大单流入序列缺失'], signal_type: null }
  H.patterns = [{ name: '红三兵', direction: 'bullish', index: 2, date: '2026-03-07' }]
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderChart(extra: Record<string, unknown> = {}) {
  return render(
    <KlineChart
      symbol="002636"
      market="CN"
      initialInterval="1d"
      initialDays={120}
      height={420}
      {...extra}
    />,
  )
}

describe('KlineChart 决策先锋辅助指标图层', () => {
  it('自取 trend-line/niuxiong + 三图例共存(操盘线/牛熊线/形态)', async () => {
    renderChart()

    await waitFor(() =>
      expect(H.calls.some((u) => u.includes('/indicators/trend-line/002636?market=CN'))).toBe(true),
    )
    expect(H.calls.some((u) => u.includes('/indicators/niuxiong/002636?market=CN'))).toBe(true)

    const trend = await screen.findByTestId('trend-line-legend')
    const nxl = await screen.findByTestId('niuxiong-legend')
    const pattern = await screen.findByTestId('pattern-legend')
    expect(trend).toBeTruthy()
    expect(nxl).toBeTruthy()
    expect(pattern).toBeTruthy()

    expect(trend.textContent).toContain('操盘线')
    expect(trend.textContent).toContain('客观标注 · 非投资建议')
    expect(nxl.textContent).toContain('牛熊线')
    expect(nxl.textContent).toContain('金叉 B')
  })

  it('图例不含买卖建议字样(信号是证据非建议)', async () => {
    renderChart()
    const trend = await screen.findByTestId('trend-line-legend')
    const nxl = await screen.findByTestId('niuxiong-legend')
    const text = `${trend.textContent} ${nxl.textContent}`
    for (const w of ['建议买入', '建议卖出', '立即买入', '推荐']) expect(text).not.toContain(w)
  })

  it('layersVisible.{trendLine,niuxiong}=false → 不发对应请求, 也不渲染对应图例', async () => {
    renderChart({ layersVisible: { trendLine: false, niuxiong: false } })
    // 等主图请求发出后再放一会儿, 确认两条辅助指标请求始终没发
    await waitFor(() => expect(H.calls.some((u) => u.includes('/002636?'))).toBe(true))
    await new Promise((r) => setTimeout(r, 600))
    expect(H.calls.some((u) => u.includes('/indicators/trend-line'))).toBe(false)
    expect(H.calls.some((u) => u.includes('/indicators/niuxiong'))).toBe(false)
    expect(screen.queryByTestId('trend-line-legend')).toBeNull()
    expect(screen.queryByTestId('niuxiong-legend')).toBeNull()
  })

  it('父传 trendLine → 不再自取 trend-line, 但仍按 prop 渲染图例', async () => {
    renderChart({
      trendLine: {
        available: true,
        lines: { red: 12, yellow: 11, green: 10 },
        band: { state: '多方带' },
        trend: '升势',
        buyPoints: [],
        sellPoints: [],
        params: {},
      },
    })
    expect(await screen.findByTestId('trend-line-legend')).toBeTruthy()
    await new Promise((r) => setTimeout(r, 500))
    expect(H.calls.some((u) => u.includes('/indicators/trend-line'))).toBe(false)
  })

  it('分时突破降级: available=false → 提示条显示「逐分钟 DDE 未接入 · 暂不出信号」', async () => {
    renderChart()
    const bar = await screen.findByTestId('minute-breakthrough-bar')
    expect(bar.textContent).toContain('逐分钟 DDE 未接入 · 暂不出信号')
    expect(bar.textContent).toContain('分时突破')
  })

  it('分时突破信号: available=true, signal_type=突 → 提示条出「突」+ 触发条件', async () => {
    H.minute = {
      available: true,
      signal_type: '突',
      trigger_time: '10:12',
      conditions: [{ name: '盘整>15分钟', met: true }],
      met_conditions: ['盘整>15分钟'],
    }
    renderChart()
    const bar = await screen.findByTestId('minute-breakthrough-bar')
    await waitFor(() => expect(bar.textContent).toContain('突'))
    expect(bar.textContent).toContain('10:12')
    expect(bar.textContent).toContain('盘整>15分钟')
  })
})
