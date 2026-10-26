// @vitest-environment jsdom
//
// perf(2026-10-02) 回归钉: 深度分析详情页的**加载链**。
//
// 背景(B1 首屏走查): 页面此前整页 early-return「加载中」, 导致 (a) 首屏只有一个裸 spinner,
// (b) 首屏 K 线主图要等 /analysis 请求返回后才挂载 → K线取数与分析请求**串行**;
// 且正文/目录每次渲染都重算(unmemoized), 滚动联动会反复重解析整页 markdown。
//
// 本测试不依赖真实网络(全部 mock), 钉住三条契约:
//   1. K线主图**不等**分析请求 —— 请求未返回时图表已挂载(并发, 而非串行);
//   2. 正文门控语义不变: 加载中 / 未找到 / 正常 三态各自显式; 历史比较慢或失败都不阻塞正文;
//   3. 无关状态变更不重算 buildAnalysisSections(滚动高亮/目录开关不应重解析 markdown)。
import { cleanup, render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// jsdom 无 IntersectionObserver(页面滚动联动用) → 桩掉
class IOStub {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() { return [] }
}
;(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = IOStub

const H = vi.hoisted(() => {
  const deferreds: Record<string, { resolve: (v: unknown) => void; reject: (e: unknown) => void; promise: Promise<unknown> }> = {}
  function defer(key: string) {
    let resolve!: (v: unknown) => void
    let reject!: (e: unknown) => void
    const promise = new Promise((res, rej) => { resolve = res; reject = rej })
    deferreds[key] = { resolve, reject, promise }
    return deferreds[key]
  }
  const klineProps: Array<{ symbol: string; hasActivity: boolean; hasGs: boolean }> = []
  let sectionsCalls = 0
  return { deferreds, defer, klineProps, sectionsCallsRef: () => sectionsCalls, bumpSections: () => { sectionsCalls += 1 }, resetSections: () => { sectionsCalls = 0 } }
})

vi.mock('@panwatch/api', () => ({
  getToken: () => 'test-token',
  fetchAPI: vi.fn(() => Promise.resolve({ klines: [], gs_signals: [], fund_flow: [], events: [], unlock_levels: [], activity_series: [] })),
  tradingAgentsApi: {
    getAnalysisByDate: vi.fn(() => H.defer('analysis').promise),
    getHistoryComparison: vi.fn(() => H.defer('history').promise),
    downloadAnalysisPdf: vi.fn(),
  },
}))

// 记录挂载与收到的图层 props(真 KlineChart 会建 lightweight-charts, jsdom 无 canvas)
vi.mock('@panwatch/biz-ui/components/KlineChart', () => ({
  default: (props: { symbol: string; activitySeries?: unknown[]; gsSignals?: unknown[] }) => {
    H.klineProps.push({
      symbol: props.symbol,
      hasActivity: Array.isArray(props.activitySeries),
      hasGs: Array.isArray(props.gsSignals),
    })
    return <div data-testid="kline-stub" />
  },
}))

vi.mock('@/hooks/useKlineLayer', () => ({
  useKlineLayer: () => ({ gsSignals: [], fundFlow: [], events: [], supportPressure: [], activitySeries: [], loaded: true }),
}))

// 真 analysis-sections + 计数包装
vi.mock('@panwatch/biz-ui/analysis-sections', async (orig) => {
  const real = (await orig()) as { buildAnalysisSections: (...a: unknown[]) => unknown }
  return {
    buildAnalysisSections: (...a: unknown[]) => { H.bumpSections(); return real.buildAnalysisSections(...a) },
  }
})

vi.mock('../../src/components/ShareCardModal', () => ({ default: () => null }))

import AnalysisDetailPage from '../../src/pages/AnalysisDetail'

const RESULT = {
  agent_name: 'tradingagents',
  title: '002361 深度分析',
  content: 'x',
  raw_data: {
    suggestion: { action: 'buy', action_label: '买入', confidence: 7 },
    cost_usd: 0.12,
    final_decision: '### 决策正文\n\n核心结论-CORE',
    analyst_reports: { market: '### 技术\n\n技术正文-MARKET', social: '', news: '', fundamentals: '' },
  },
  timestamp: '2026-08-06',
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/analysis/002361/2026-08-06']}>
      <Routes>
        <Route path="/analysis/:symbol/:date" element={<AnalysisDetailPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  H.klineProps.length = 0
  H.resetSections()
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  delete H.deferreds.analysis
  delete H.deferreds.history
})

describe('AnalysisDetail 加载链(perf 回归)', () => {
  it('K线主图不等分析请求: 请求未返回时图表已挂载(并发, 非串行)', () => {
    renderPage()
    // 分析请求仍在 pending, 但首屏图表已挂载
    expect(H.deferreds.analysis).toBeTruthy()
    expect(screen.getByTestId('kline-stub')).toBeTruthy()
    expect(H.klineProps[0].symbol).toBe('002361')
  })

  it('图层以 props 交给图表(activitySeries 亦传入, 图表不再自取 summary)', () => {
    renderPage()
    expect(H.klineProps[0].hasActivity).toBe(true)
    expect(H.klineProps[0].hasGs).toBe(true)
  })

  it('加载中: 正文区显式提示, 不显示「未找到」', () => {
    renderPage()
    expect(screen.getByText('加载中...')).toBeTruthy()
    expect(screen.queryByText(/未找到/)).toBeNull()
  })

  it('历史比较仍 pending → 正文已渲染(不阻塞); 历史到达后独立更新', async () => {
    renderPage()
    await act(async () => { H.deferreds.analysis.resolve(RESULT) })
    // 历史请求尚未 resolve, 正文已可见 —— 次级数据不阻塞核心正文
    await waitFor(() => expect(screen.getByText('核心结论-CORE')).toBeTruthy())
    expect((H.deferreds.history as { promise: Promise<unknown> }).promise).toBeTruthy()

    await act(async () => {
      H.deferreds.history.resolve({
        items: [{
          trace_id: '', analysis_date: '2026-07-01', action: 'buy', action_label: '买入',
          confidence: 6, cost_usd: 0.1, price_at_analysis: 9.5,
          return_1d_pct: 1.2, return_5d_pct: 2.3, return_20d_pct: 3.4, hit_20d: true,
        }],
        stats: {},
      })
    })
    await waitFor(() => expect(screen.getByText('2026-07-01')).toBeTruthy())
    // 正文逐字不变
    expect(screen.getByText('核心结论-CORE')).toBeTruthy()
  })

  it('历史比较失败 → 正文结果不变, 历史区显式空态(不阻塞、不假装)', async () => {
    renderPage()
    await act(async () => { H.deferreds.analysis.resolve(RESULT) })
    await waitFor(() => expect(screen.getByText('核心结论-CORE')).toBeTruthy())

    await act(async () => { H.deferreds.history.reject(new Error('history-down')) })
    await waitFor(() => expect(screen.getByText('暂无历史决策记录')).toBeTruthy())
    // 正文逐字不变
    expect(screen.getByText('核心结论-CORE')).toBeTruthy()
    expect(screen.getByText('技术正文-MARKET')).toBeTruthy()
  })

  it('分析无记录(null) → 显示「未找到」, 且不挂载图表', async () => {
    renderPage()
    await act(async () => { H.deferreds.analysis.resolve(null) })
    await waitFor(() => expect(screen.getByText(/未找到/)).toBeTruthy())
    expect(screen.queryByTestId('kline-stub')).toBeNull()
  })

  it('无关状态变更(目录开关/滚动高亮)不重算 buildAnalysisSections', async () => {
    renderPage()
    await act(async () => { H.deferreds.analysis.resolve(RESULT) })
    await waitFor(() => expect(screen.getByText('核心结论-CORE')).toBeTruthy())

    const afterLoad = H.sectionsCallsRef()
    // 点二级目录开关 → 触发一次重渲染
    await act(async () => { fireEvent.click(screen.getByText('二级目录')) })
    expect(H.sectionsCallsRef()).toBe(afterLoad)
  })
})
