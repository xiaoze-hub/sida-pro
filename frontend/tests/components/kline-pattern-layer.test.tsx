// @vitest-environment jsdom
//
// K线形态图层(2026-10-10) 渲染回归: 真渲染 KlineChart(canvas 层换成假图表库, 见 vite.config test.alias),
// 只 mock 取数, 钉四件事:
//  ① 自取: 组件向 `GET /klines/{symbol}/patterns?market=…` 取形态(父不传时);
//  ② 渲染: 取到形态 → 图例(`pattern-legend`)出现, 含形态名与"客观标注 · 非投资建议";
//  ③ 门控: `layersVisible.pattern=false` → **不发** /patterns 请求, 也不渲染图例;
//  ④ 父接管: 传了 `patterns` prop → 不再自取, 直接渲染。
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
  patterns: [] as Array<Record<string, unknown>>,
}))

vi.mock('@panwatch/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string) => {
    H.calls.push(String(url))
    if (String(url).includes('/patterns')) {
      return Promise.resolve({ symbol: '002636', count: H.patterns.length, patterns: H.patterns })
    }
    if (String(url).includes('/summary')) return Promise.resolve({})
    // 主图 K 线: 给几根有限 OHLC, 组件内过滤后 setData
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

const PATTERNS = [
  {
    name: '红三兵',
    direction: 'bullish',
    category: '持续',
    index: 2,
    date: '2026-03-07',
    position: '趋势中',
    basis: ['三连阳且收盘递增'],
    definition: '三连阳且收盘递增。',
  },
  {
    name: '黄昏之星',
    direction: 'bearish',
    category: '反转',
    index: 2,
    date: '2026-03-07',
    position: '高位',
    basis: ['首根长阳'],
    definition: '长阳→星线→长阴。',
  },
]

beforeEach(() => {
  H.calls.length = 0
  H.patterns = PATTERNS
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('KlineChart K线形态图层', () => {
  it('自取 /patterns 并渲染图例(形态名 + 客观标注提示)', async () => {
    render(<KlineChart symbol="002636" market="CN" initialInterval="1d" initialDays={120} height={420} />)

    await waitFor(() => expect(H.calls.some((u) => u.includes('/002636/patterns?market=CN'))).toBe(true))
    const legend = await screen.findByTestId('pattern-legend')
    expect(legend).toBeTruthy()
    expect(screen.getByText('红三兵')).toBeTruthy()
    expect(screen.getByText('黄昏之星')).toBeTruthy()
    expect(screen.getByText('客观标注 · 非投资建议')).toBeTruthy()
    // 悬停详情: 形态定义挂在 title 上(客观描述)
    expect(screen.getByTitle('三连阳且收盘递增。')).toBeTruthy()
  })

  it('图例不含买卖建议字样(证据非建议)', async () => {
    render(<KlineChart symbol="002636" market="CN" initialDays={120} height={420} />)
    const legend = await screen.findByTestId('pattern-legend')
    const text = legend.textContent || ''
    for (const w of ['建议买入', '建议卖出', '立即买入', '推荐']) expect(text).not.toContain(w)
  })

  it('layersVisible.pattern=false → 不发 /patterns 请求, 也不渲染图例', async () => {
    render(
      <KlineChart
        symbol="002636"
        market="CN"
        initialDays={120}
        height={420}
        layersVisible={{ pattern: false }}
      />,
    )
    // 等主图请求发出后再放一会儿, 确认 /patterns 始终没有发
    await waitFor(() => expect(H.calls.some((u) => u.includes('/002636?'))).toBe(true))
    await new Promise((r) => setTimeout(r, 600))
    expect(H.calls.some((u) => u.includes('/patterns'))).toBe(false)
    expect(screen.queryByTestId('pattern-legend')).toBeNull()
  })

  it('父传 patterns → 不再自取, 直接渲染', async () => {
    render(
      <KlineChart
        symbol="002636"
        market="CN"
        initialDays={120}
        height={420}
        patterns={[{ name: '金针探底', direction: 'bullish', index: 2, date: '2026-03-07' }]}
      />,
    )
    expect(await screen.findByText('金针探底')).toBeTruthy()
    await new Promise((r) => setTimeout(r, 500))
    expect(H.calls.some((u) => u.includes('/patterns'))).toBe(false)
  })
})
