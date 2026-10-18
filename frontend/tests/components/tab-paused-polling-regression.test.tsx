// @vitest-environment jsdom
/**
 * 页签 keep-alive 成本闸门 —— **页面级**回归(2026-10-15)。
 *
 * `tab-activity-gate.test.tsx` 守的是**共享数据层**(usePolling / useApiQuery / useQuoteStream),
 * `use-paused-interval.test.tsx` 守的是 hook 本身。本文件补最后一层: **真实页面组件**
 * (页签式工作台里同时挂载、非活动页签只 `display:none`)在隐藏时**一个轮询请求都不发**,
 * 切回前台**立即补一次**(不等下一个周期)。
 *
 * 三个页面各钉一个"只有它自己会打"的端点(排除其他组件误伤):
 *   ① `ThemeMood`(功能页签)      → `/theme-mood/ladder`(连板梯队 60s 轮询);
 *   ② `L2Tab`(工作台标签)        → `/orderbook-ob` + `/seal-quality`(30s 轮询);
 *   ③ `BoardHeatmap`(首页板块热力图) → `/boards/heatmap`(POLL_MS 轮询)。
 *
 * 断言语义: 隐藏 = **0 次**(不是"变少"); 可见 = 挂载即补一次(首拉)。
 */
import { cleanup, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'
import { TabActiveProvider } from '@panwatch/base-ui'

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  quote: vi.fn(),
  moreInfo: vi.fn(),
  darkFlowTq: vi.fn(),
  klineSummary: vi.fn(),
  klines: vi.fn(),
  portfolioSummary: vi.fn(),
  suggestions: vi.fn(),
  news: vi.fn(),
  history: vi.fn(),
  company: vi.fn(),
  orderbookOb: vi.fn(),
  sealQuality: vi.fn(),
  stocksList: vi.fn(),
  stocksCreate: vi.fn(),
  stocksRemove: vi.fn(),
  stocksUpdateAgents: vi.fn(),
  triggerAgent: vi.fn(),
  getLatestForStock: vi.fn(),
  getHistoryComparison: vi.fn(),
  fundamentalsDetail: vi.fn(),
  setOption: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
  insightApi: {
    quote: (...a: unknown[]) => mocks.quote(...a),
    moreInfo: (...a: unknown[]) => mocks.moreInfo(...a),
    darkFlowTq: (...a: unknown[]) => mocks.darkFlowTq(...a),
    klineSummary: (...a: unknown[]) => mocks.klineSummary(...a),
    klines: (...a: unknown[]) => mocks.klines(...a),
    portfolioSummary: (...a: unknown[]) => mocks.portfolioSummary(...a),
    suggestions: (...a: unknown[]) => mocks.suggestions(...a),
    news: (...a: unknown[]) => mocks.news(...a),
    history: (...a: unknown[]) => mocks.history(...a),
    company: (...a: unknown[]) => mocks.company(...a),
    orderbookOb: (...a: unknown[]) => mocks.orderbookOb(...a),
    sealQuality: (...a: unknown[]) => mocks.sealQuality(...a),
  },
  stocksApi: {
    list: (...a: unknown[]) => mocks.stocksList(...a),
    create: (...a: unknown[]) => mocks.stocksCreate(...a),
    remove: (...a: unknown[]) => mocks.stocksRemove(...a),
    updateAgents: (...a: unknown[]) => mocks.stocksUpdateAgents(...a),
    triggerAgent: (...a: unknown[]) => mocks.triggerAgent(...a),
  },
  tradingAgentsApi: {
    getLatestForStock: (...a: unknown[]) => mocks.getLatestForStock(...a),
    getHistoryComparison: (...a: unknown[]) => mocks.getHistoryComparison(...a),
  },
  fundamentalsApi: { detail: (...a: unknown[]) => mocks.fundamentalsDetail(...a) },
}))

vi.mock('@panwatch/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, on: vi.fn(), off: vi.fn(), resize: vi.fn() } },
  }),
}))

import ThemeMoodPage from '@/pages/ThemeMood'
import L2Tab from '@/pages/workbench/tabs/L2Tab'
import BoardHeatmap from '@panwatch/biz-ui/components/dashboard/BoardHeatmap'

/** 让"隐藏时确实没发"有个可观测的时间窗(不用假定时器 —— 这些页面还带图表/动画)。 */
const settle = () => new Promise((r) => setTimeout(r, 60))

/** 某 URL 片段被请求了几次 */
const hits = (frag: string) =>
  mocks.fetchAPI.mock.calls.filter((c) => String(c[0]).includes(frag)).length

beforeEach(() => {
  vi.clearAllMocks()
  mocks.fetchAPI.mockResolvedValue({ items: [], accounts: [], ladder: [], klines: [] })
  mocks.quote.mockResolvedValue({ symbol: '002636', market: 'CN', name: '测试标的', current_price: 11.8 })
  mocks.moreInfo.mockResolvedValue({ symbol: '002636', market: 'CN', raw: {} })
  mocks.darkFlowTq.mockResolvedValue(null)
  mocks.klineSummary.mockResolvedValue({ summary: null })
  mocks.klines.mockResolvedValue({ klines: [] })
  mocks.portfolioSummary.mockResolvedValue({ accounts: [] })
  mocks.suggestions.mockResolvedValue([])
  mocks.news.mockResolvedValue([])
  mocks.history.mockResolvedValue([])
  mocks.company.mockResolvedValue({})
  mocks.stocksList.mockResolvedValue([])
  mocks.triggerAgent.mockResolvedValue({})
  mocks.getLatestForStock.mockResolvedValue(null)
  mocks.getHistoryComparison.mockResolvedValue(null)
  mocks.fundamentalsDetail.mockResolvedValue({})
  mocks.orderbookOb.mockResolvedValue({ available: false, ob_series: [], events: [], ghost_ratio: 0, note: '无' })
  mocks.sealQuality.mockResolvedValue({ symbol: '002636', n_samples: 0, metrics: { available: false } })
})
afterEach(() => cleanup())

describe('① ThemeMood 页(功能页签): 隐藏不发 /theme-mood/ladder, 回前台立即补一次', () => {
  it('隐藏 → 0 次; 切可见 → 立即补一次(不等 60s 周期)', async () => {
    const { rerender } = render(
      <TabActiveProvider active={false}>
        <ThemeMoodPage />
      </TabActiveProvider>,
    )
    await settle()
    expect(hits('/theme-mood/ladder')).toBe(0)

    rerender(
      <TabActiveProvider active>
        <ThemeMoodPage />
      </TabActiveProvider>,
    )
    await waitFor(() => expect(hits('/theme-mood/ladder')).toBeGreaterThanOrEqual(1))
  })
})

describe('② L2Tab(工作台标签): 隐藏不发盘口轮询端点', () => {
  const tab = () => (
    <MemoryRouter>
      <ToastProvider>
        <L2Tab symbol="002636" market="CN" hasPosition={false} />
      </ToastProvider>
    </MemoryRouter>
  )

  it('隐藏 → orderbookOb / sealQuality 一次都没打; 切可见 → 各打一次', async () => {
    const { rerender } = render(<TabActiveProvider active={false}>{tab()}</TabActiveProvider>)
    await settle()
    expect(mocks.orderbookOb).not.toHaveBeenCalled()
    expect(mocks.sealQuality).not.toHaveBeenCalled()

    rerender(<TabActiveProvider active>{tab()}</TabActiveProvider>)
    await waitFor(() =>
      expect(mocks.orderbookOb).toHaveBeenCalledWith('002636', expect.anything()),
    )
    expect(mocks.sealQuality).toHaveBeenCalledWith('002636', expect.anything())
  })
})

describe('③ BoardHeatmap(首页板块热力图): 隐藏不发 /boards/heatmap', () => {
  it('隐藏 → 0 次; 切可见 → 立即补一次', async () => {
    const { rerender } = render(
      <TabActiveProvider active={false}>
        <BoardHeatmap onOpenBoard={() => {}} />
      </TabActiveProvider>,
    )
    await settle()
    expect(hits('/boards/heatmap')).toBe(0)

    rerender(
      <TabActiveProvider active>
        <BoardHeatmap onOpenBoard={() => {}} />
      </TabActiveProvider>,
    )
    await waitFor(() => expect(hits('/boards/heatmap')).toBeGreaterThanOrEqual(1))
  })
})
