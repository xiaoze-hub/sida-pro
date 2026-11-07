// @vitest-environment jsdom
/**
 * 指数 K 线独立复审(2026-11-07)的**本轮修复**回归钉 —— 真 `KlineChart`, 只换 canvas 层。
 *
 * 与 `kline-index-instrument.test.tsx`(上一轮: 取数边界)互补, 这里钉本轮复审证实、上一轮没覆盖的:
 *  ① 指数分钟 K 陷阱: 后端对 `interval=1m|5m|15m|30m|60m` **回传日 K**(实测) —— 指数图
 *     不给分钟按钮、父传 `initialInterval='5m'` 也被钳成 `1d`、任何路径都不发指数分钟请求;
 *  ② 周期选择真的能切: `initialInterval` 是**初始值**, 不许把用户点出来的周/月 K 打回日 K
 *     (旧 effect 只要父传了常量就一直重置 → 指数页永远停在日 K);
 *  ③ 父传的个股专属线一律不画到指数上: `supportPressure` / 时间点标注 / 交割单 / 成本线;
 *     而个股图照旧画, 且错误价位(NaN/0/负/未知方向)一条都不画;
 *  ④ 筹码面板收起或无数据时**不留 152px 空白**(旧写法只按开关留槽位)。
 */
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __priceLines, __resetPriceLines } from '../stubs/lightweight-charts'

// jsdom 无 ResizeObserver(KlineChart 容器尺寸自适应用) → 桩掉
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

const H = vi.hoisted(() => ({
  calls: [] as string[],
  /** summary 里的主力筹码沉淀(用例决定给不给) */
  chips: null as null | Record<string, unknown>,
}))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchScenario: vi.fn(async () => ({ available: false })),
  fetchAPI: vi.fn(async (url: string) => {
    const u = String(url)
    H.calls.push(u)
    if (u.includes('/summary')) {
      return H.chips ? { main_accumulation_chips: H.chips } : {}
    }
    if (/^\/klines\/[^/?]+\?/.test(u)) {
      return {
        klines: [
          { date: '2026-10-30', open: 3200, high: 3210, low: 3190, close: 3205, volume: 1000 },
          { date: '2026-11-02', open: 3205, high: 3220, low: 3200, close: 3215, volume: 1100 },
          { date: '2026-11-03', open: 3215, high: 3230, low: 3210, close: 3225, volume: 1200 },
        ],
      }
    }
    return {}
  }),
}))

import KlineChart from '@sida/biz-ui/components/KlineChart'

function renderChart(props: Record<string, unknown> = {}) {
  return render(
    <KlineChart symbol="000001" market="CN" initialInterval="1d" initialDays={120} height={420} {...props} />,
  )
}

/** 等慢车道(SUMMARY_SLOW_LANE_DELAY_MS = 350ms)窗口过去 */
async function settleSlowLane() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 600))
  })
}

const MINUTE_URL = /interval=(1m|5m|15m|30m|60m)/

beforeEach(() => {
  H.calls.length = 0
  H.chips = null
  __resetPriceLines()
  try { localStorage.clear() } catch { /* ignore */ }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('指数图 · 分钟 K 陷阱(后端回传日 K)', () => {
  it('不给分钟档按钮: 只有 日K/周K/月K', async () => {
    renderChart({ instrument: 'index' })
    await waitFor(() => expect(screen.getByText('3 根K线')).toBeTruthy())

    expect(screen.getByText('日K')).toBeTruthy()
    expect(screen.getByText('周K')).toBeTruthy()
    expect(screen.getByText('月K')).toBeTruthy()
    for (const minute of ['1分', '5分', '15分', '30分', '60分']) {
      expect(screen.queryByText(minute)).toBeNull()
    }
  })

  it('父传 initialInterval=5m 也被钳成日 K: 不发、不标分钟周期', async () => {
    renderChart({ instrument: 'index', initialInterval: '5m' })
    await waitFor(() => expect(screen.getByText('3 根K线')).toBeTruthy())
    await settleSlowLane()

    expect(H.calls.filter((u) => MINUTE_URL.test(u))).toEqual([])
    expect(H.calls[0]).toBe('/klines/000001?market=CN&days=120&interval=1d&instrument=index')
    expect(screen.queryByText('5分')).toBeNull()
  })
})

describe('个股图 · 分钟档同步下线(后端无分钟K, 不许发伪请求)', () => {
  // 2026-11-07: 后端 `/klines/{symbol}?interval=5m` 过去把日K 回传并标成 "5m"(伪标),
  // 现在改为 400 显式拒绝 —— 图表侧必须一起收口: 不给按钮、不发请求、不显示"5分"。
  it('个股图也只有 日K/周K/月K, 一分钟请求都不发', async () => {
    renderChart({}) // instrument 未传 = 个股
    await waitFor(() => expect(screen.getByText('3 根K线')).toBeTruthy())
    await settleSlowLane()

    expect(screen.getByText('日K')).toBeTruthy()
    expect(screen.getByText('周K')).toBeTruthy()
    expect(screen.getByText('月K')).toBeTruthy()
    for (const minute of ['1分', '5分', '15分', '30分', '60分']) {
      expect(screen.queryByText(minute)).toBeNull()
    }
    expect(H.calls.filter((u) => MINUTE_URL.test(u))).toEqual([])
  })
})

describe('指数图 · 周期选择不被 initialInterval 钳住', () => {
  it('点「周K」真的切过去(旧实现会被父传的常量 1d 打回日 K)', async () => {
    renderChart({ instrument: 'index' })
    await waitFor(() => expect(screen.getByText('3 根K线')).toBeTruthy())
    expect(screen.getByText('日K').className).toContain('bg-primary')

    await act(async () => {
      screen.getByText('周K').click()
    })

    await waitFor(() => expect(H.calls.some((u) => u.includes('interval=1w'))).toBe(true))
    expect(screen.getByText('周K').className).toContain('bg-primary')
    expect(screen.getByText('日K').className).not.toContain('bg-primary')
    // 全程没有分时/分钟请求被这条路径带出来
    expect(H.calls.filter((u) => MINUTE_URL.test(u))).toEqual([])
  })
})

describe('指数图 · 父传个股专属图层不画', () => {
  it('supportPressure / 时间点标注 / 交割单 / 成本线一条都不上图', async () => {
    renderChart({
      instrument: 'index',
      supportPressure: [
        { price: 10.5, kind: 'support', label: '个股支撑' },
        { price: 12.0, kind: 'pressure', label: '个股压力' },
      ],
      annotationMarkers: [
        { annotationId: 1, symbol: '000001', date: '2026-11-02', label: '个股标注' },
      ],
      tradeMarkers: [{ date: '2026-11-02', side: 'buy', text: '买' }],
      costLines: [{ price: 9.9, title: '持仓成本' }],
    })
    await waitFor(() => expect(screen.getByText('3 根K线')).toBeTruthy())
    await settleSlowLane()

    // 指数图: 宁可空, 不可把「平安银行」的价位/成交标到上证指数上
    expect(__priceLines).toHaveLength(0)
    expect(screen.queryByTestId('gs-legend')).toBeNull()
  })

  it('个股图照旧画合法价位线, 错误价位一律不画', async () => {
    renderChart({
      supportPressure: [
        { price: 10.5, kind: 'support', label: '撑' },
        { price: Number.NaN, kind: 'pressure', label: 'NaN' },
        { price: 0, kind: 'support', label: '零价' },
        { price: -1, kind: 'pressure', label: '负价' },
        { price: 11, kind: 'unknown', label: '未知方向' },
      ],
    })
    await waitFor(() => expect(screen.getByText('3 根K线')).toBeTruthy())
    await waitFor(() => expect(__priceLines.length).toBeGreaterThan(0))

    const drawn = __priceLines as Array<{ price: number; title?: string }>
    // 有效价位会因图层 effect 重跑被重建(先 removePriceLine 再建), 故按**价位集合**断言:
    // 只剩 10.5 这一条, 其余脏值一条都没进图。
    expect(drawn.length).toBeGreaterThan(0)
    expect([...new Set(drawn.map((l) => l.price))]).toEqual([10.5])
    expect(drawn.every((l) => l.title === '撑')).toBe(true)
  })
})

describe('筹码面板 · 收起/无数据不留 152px 空白', () => {
  it('开关关着: 画布槽位 0', async () => {
    renderChart()
    await waitFor(() => expect(screen.getByText('3 根K线')).toBeTruthy())
    expect(screen.getByTestId('kline-canvas-slot').style.paddingRight).toBe('0px')
  })

  it('开关开着但没拿到筹码数据: 不留空白, 也不画空面板', async () => {
    localStorage.setItem('sida_chip_profile', '1')
    renderChart()
    await waitFor(() => expect(screen.getByText('3 根K线')).toBeTruthy())
    await settleSlowLane()

    expect(screen.queryByTestId('chip-profile-histogram')).toBeNull()
    expect(screen.getByTestId('kline-canvas-slot').style.paddingRight).toBe('0px')
  })

  it('开关开着且真有筹码数据: 才留 152px 并画出面板', async () => {
    localStorage.setItem('sida_chip_profile', '1')
    H.chips = { prices: [10, 10.5, 11], chips: [100, 300, 120], main_cost: 10.5, last_close: 10.8 }
    renderChart()
    await waitFor(() => expect(screen.getByTestId('chip-profile-histogram')).toBeTruthy())
    expect(screen.getByTestId('kline-canvas-slot').style.paddingRight).toBe('152px')
  })

  it('指数图即使 localStorage 里开过筹码开关, 也不留空白', async () => {
    localStorage.setItem('sida_chip_profile', '1')
    renderChart({ instrument: 'index' })
    await waitFor(() => expect(screen.getByText('3 根K线')).toBeTruthy())
    await settleSlowLane()

    expect(screen.getByTestId('kline-canvas-slot').style.paddingRight).toBe('0px')
    expect(screen.queryByText(/筹码分布/)).toBeNull()
  })
})
