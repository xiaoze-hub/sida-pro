// @vitest-environment jsdom
//
// 加载韧性(2026-10-10): AnalysisDetail 的失败态 + 重试。
//
// 背景: 此前 `.catch(() => setResult(null))` 把「后端闪断/超时」**误显示成「未找到记录」**,
// 用户以为没有数据、只能刷新浏览器。修复: 失败态显式(ErrorState) + 「重试」按钮。
//
// 本测试禁真实网络(全 mock), 钉住:
//   1. 分析请求失败 → 显式错误态(role=alert) + 「重试」按钮, **不显示「未找到」**;
//   2. 点「重试」→ 重新请求; 第二次成功 → 正文上屏, 错误态消失。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class IOStub {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() { return [] }
}
;(globalThis as unknown as { IntersectionObserver: unknown }).IntersectionObserver = IOStub

const H = vi.hoisted(() => ({ calls: 0, mode: 'fail-first' as 'fail-first' | 'always-fail' }))

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

vi.mock('@panwatch/api', () => ({
  getToken: () => 'test-token',
  fetchAPI: vi.fn(() => Promise.resolve({ klines: [], gs_signals: [], fund_flow: [], events: [], unlock_levels: [], activity_series: [] })),
  tradingAgentsApi: {
    getAnalysisByDate: vi.fn(() => {
      H.calls += 1
      if (H.mode === 'always-fail' || H.calls === 1) {
        // 类型化超时错误(与 fetchAPI 抛出的形状一致)
        return Promise.reject(Object.assign(new Error('请求超时，请稍后重试'), { kind: 'TIMEOUT' }))
      }
      return Promise.resolve(RESULT)
    }),
    getHistoryComparison: vi.fn(() => Promise.resolve({ items: [], stats: {} })),
    downloadAnalysisPdf: vi.fn(),
  },
}))

vi.mock('@panwatch/biz-ui/components/KlineChart', () => ({ default: () => <div data-testid="kline-stub" /> }))
vi.mock('@/hooks/useKlineLayer', () => ({
  useKlineLayer: () => ({ gsSignals: [], fundFlow: [], events: [], supportPressure: [], activitySeries: [], loaded: true }),
}))
vi.mock('../../src/components/ShareCardModal', () => ({ default: () => null }))

import AnalysisDetailPage from '../../src/pages/AnalysisDetail'

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
  H.calls = 0
  H.mode = 'fail-first'
})
afterEach(() => cleanup())

describe('AnalysisDetail 失败态 + 重试(加载韧性)', () => {
  it('分析请求失败 → 显式错误态 + 重试按钮, 不显示「未找到」(失败≠没有记录)', async () => {
    renderPage()
    const alert = await screen.findByRole('alert')
    // 类型化超时 → ErrorState 归类为「请求超时」
    expect(alert.textContent).toContain('请求超时')
    expect(screen.getByText('重试')).toBeTruthy()
    // 关键: 不能把失败伪装成「未找到」
    expect(screen.queryByText(/未找到/)).toBeNull()
  })

  it('点「重试」重新取数: 第二次成功 → 正文上屏, 错误态消失', async () => {
    renderPage()
    await screen.findByRole('alert')
    expect(H.calls).toBe(1)

    await act(async () => {
      fireEvent.click(screen.getByText('重试'))
    })
    await waitFor(() => expect(screen.getByText('核心结论-CORE')).toBeTruthy())
    expect(H.calls).toBe(2)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('持续失败(重试后仍失败) → 错误态保留, 不会永远转圈', async () => {
    H.mode = 'always-fail'
    renderPage()
    await screen.findByRole('alert')
    await act(async () => {
      fireEvent.click(screen.getByText('重试'))
    })
    await waitFor(() => expect(H.calls).toBe(2))
    expect(screen.getByRole('alert')).toBeTruthy()
    expect(screen.queryByText('加载中...')).toBeNull()
  })
})
