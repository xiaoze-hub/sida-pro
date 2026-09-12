// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))
vi.mock('@panwatch/api', () => ({ fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a) }))

import ThemeMoodPage from '@/pages/ThemeMood'

const RESP = {
  trade_date: '20260911',
  window: 20,
  count: 2,
  dates: ['20260831', '20260901', '20260910', '20260911'],
  market: [
    { date: '20260831', score: 71.5 },
    { date: '20260901', score: 83.8 },
    { date: '20260910', score: 69.6 },
    { date: '20260911', score: 71.7 },
  ],
  items: [
    {
      block_code: '881101.SH', block_name: '元件', block_type: 'industry', score: 78.2, delta: 4.1,
      confidence: 86, core: true, s1: 80, s2: 75, s3: 82, s4: 70, s5: 60, limit_up_cnt: 9, max_boards: 3,
      core_stocks: [{ symbol: '600001.SH', name: '甲股', boards: 3, pct: 10.0, score: 88.4, prob: 0.62 }],
      cells: [
        { date: '20260831', score: 58.0, limit_up_cnt: 3 },
        { date: '20260901', score: 64.0, limit_up_cnt: 5 },
        { date: '20260910', score: 74.1, limit_up_cnt: 7 },
        { date: '20260911', score: 78.2, limit_up_cnt: 9 },
      ],
    },
    {
      block_code: '880301.SH', block_name: '某概念', block_type: 'concept', score: 61.0, delta: -2.0,
      confidence: 60, core: false, s1: 60, s2: 62, s3: null, s4: 50, s5: 55, limit_up_cnt: 3, max_boards: 2,
      core_stocks: [], cells: [{ date: '20260911', score: 61.0, limit_up_cnt: 3 }],
    },
  ],
}

describe('ThemeMood 页面', () => {
  afterEach(() => cleanup())
  beforeEach(() => mocks.fetchAPI.mockReset())

  it('渲染榜单与核心标记', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<ThemeMoodPage />)
    expect((await screen.findAllByText('元件')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('78.2').length).toBeGreaterThan(0)
    expect(screen.getByText('核心')).toBeTruthy()
  })

  it('按 window=20 请求', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<ThemeMoodPage />)
    await screen.findAllByText('元件')
    // 页面同时拉了市场级情绪周期(segments/stats), 只断言榜单那次请求
    const boardCall = mocks.fetchAPI.mock.calls.map((c) => String(c[0])).find((u) => u.includes('/theme-mood/board'))
    expect(boardCall).toContain('window=20')
  })

  it('时间轴: 渲染月份带与日号, 缺该交易日的题材显示空位', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<ThemeMoodPage />)
    await screen.findAllByText('元件')
    expect(screen.getByText('8月')).toBeTruthy()
    expect(screen.getByText('9月')).toBeTruthy()
    expect(screen.getByText('8/31')).toBeTruthy()
    expect(screen.getByText('9/1')).toBeTruthy()
    // 某概念只有 20260911 一天 → 其余三列渲染 '--' 占位
    expect(screen.getAllByText('--').length).toBeGreaterThanOrEqual(3)
  })

  it('走势曲线: 顶部渲染前20均值曲线并标最新值', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const { container } = render(<ThemeMoodPage />)
    await screen.findAllByText('元件')
    expect(screen.getByText('情绪走势')).toBeTruthy()
    expect(screen.getByText('71.7')).toBeTruthy()
    // 只数图表 svg(role=img), 排除 lucide 图标里的 polyline/path
    const charts = () => container.querySelectorAll('svg[role="img"]')
    expect(charts()).toHaveLength(1)
    expect(charts()[0].querySelectorAll('polyline')).toHaveLength(1)
    expect(charts()[0].querySelector('polyline')?.getAttribute('points')).toBe('19,35.5 59,6 99,40 139,35')
    expect(charts()[0].querySelectorAll('path')).toHaveLength(1)
  })

  it('走势曲线: 点选题材后明细里画出该题材曲线', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const { container } = render(<ThemeMoodPage />)
    const names = await screen.findAllByText('元件')
    fireEvent.click(names[0].closest('button') as HTMLButtonElement)
    expect(screen.getByText('情绪走势(近 4 个交易日)')).toBeTruthy()
    // 该题材自身曲线(58/64/74.1/78.2 → 最高78.2 最低58.0), 与顶部曲线各占一条折线
    expect(screen.getByText('最高 78.2 · 最低 58.0 · 最新', { exact: false })).toBeTruthy()
    const polys = container.querySelectorAll('svg[role="img"] polyline')
    expect(polys).toHaveLength(2)
    expect(polys[1].getAttribute('points')).toBe('19,62 59,45.4 99,17.4 139,6')
  })

  it('时间轴加载后默认滚到最新一端', async () => {
    const proto = HTMLElement.prototype
    const origW = Object.getOwnPropertyDescriptor(proto, 'scrollWidth')
    const origL = Object.getOwnPropertyDescriptor(proto, 'scrollLeft')
    const writes: number[] = []
    Object.defineProperty(proto, 'scrollWidth', { configurable: true, get: () => 800 })
    Object.defineProperty(proto, 'scrollLeft', {
      configurable: true,
      get: () => writes[writes.length - 1] ?? 0,
      set: (v: number) => {
        writes.push(v)
      },
    })
    try {
      mocks.fetchAPI.mockResolvedValue(RESP)
      render(<ThemeMoodPage />)
      await screen.findAllByText('元件')
      expect(writes).toContain(800)
    } finally {
      if (origW) Object.defineProperty(proto, 'scrollWidth', origW)
      if (origL) Object.defineProperty(proto, 'scrollLeft', origL)
    }
  })
})

const LADDER = {
  dates: ['20260911'],
  ladder: [
    {
      date: '20260911',
      rows: [
        { boards: 1, codes: ['D'], names: ['DD'], tag: '首板', stocks: [{ symbol: 'D', name: 'DD', candle: null }] },
      ],
      blown: [], broken: [],
    },
  ],
  mode: 'finalized', stale: false, degraded: null, live_day: null,
}

describe('ThemeMood 布局(v0.5.85)', () => {
  afterEach(() => { cleanup(); localStorage.clear() })
  beforeEach(() => mocks.fetchAPI.mockReset())

  const mountBoth = async () => {
    mocks.fetchAPI.mockImplementation((url: string) =>
      String(url).includes('/theme-mood/ladder') ? Promise.resolve(LADDER) : Promise.resolve(RESP))
    render(<ThemeMoodPage />)
    await screen.findAllByText('元件')
  }

  it('梯队在题材表之前(打开即见)', async () => {
    await mountBoth()
    const ladder = screen.getByText('连板梯队')
    const board = screen.getByText('题材')
    // ladder 在 board 之前 => board 相对 ladder 是 FOLLOWING
    expect(ladder.compareDocumentPosition(board) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('题材表可折叠且记忆到 localStorage', async () => {
    await mountBoth()
    const collapse = screen.getByRole('button', { name: '折叠题材表' })
    fireEvent.click(collapse)
    expect(localStorage.getItem('tm-board-collapsed')).toBe('1')
    expect(screen.getByRole('button', { name: '展开题材表' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '展开题材表' }))
    expect(localStorage.getItem('tm-board-collapsed')).toBe('0')
  })
})

describe('ThemeMood 盘中实时(v0.5.87)', () => {
  afterEach(() => { cleanup(); localStorage.clear() })
  beforeEach(() => mocks.fetchAPI.mockReset())

  it('live 模式显示盘中角标与收盘撮合提示', async () => {
    const liveLadder = {
      dates: ['20260911'],
      ladder: [],
      mode: 'live',
      live_day: { date: '20260912', rows: [], blown: [], broken: [], provisional: true },
      stale: false,
      degraded: null,
      note_closing: '收盘撮合中, 稍后定型',
    }
    mocks.fetchAPI.mockImplementation((url: string) =>
      String(url).includes('/theme-mood/ladder') ? Promise.resolve(liveLadder) : Promise.resolve(RESP))
    render(<ThemeMoodPage />)
    expect(await screen.findByText('盘中实时(60s)')).toBeTruthy()
    expect(screen.getByText('收盘撮合中, 稍后定型')).toBeTruthy()
    expect(screen.getByText('盘中')).toBeTruthy()  // live_day provisional 角标
  })
})
