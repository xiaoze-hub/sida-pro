// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 三指标共振清单卡片 (2026-09-11 决策先锋升级 B): 读 /resonance/scan, 切 共振/接近 两个口径。
const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn(), navigate: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('react-router-dom', () => ({
  useNavigate: () => mocks.navigate,
}))

import ResonancePanel from '@panwatch/biz-ui/components/dashboard/ResonancePanel'

const RESP = {
  trade_date: '20260911',
  count: 2,
  items: [
    { trade_date: '20260911', symbol: '002361', name: '神剑股份', trend: 'G区间', activity: 4.31, level: '强势', fund_net: 2.5e8, hits: 3, resonance: true, near: false, close: 12.3, change_pct: 3.21, ai_verdict: '强共振', ai_summary: '三项齐备', ai_confidence: 0.91 },
    { trade_date: '20260911', symbol: '300750', name: '宁德时代', trend: 'G区间', activity: 3.1, level: '强势', fund_net: -1e7, hits: 2, resonance: false, near: true, close: 210.0, change_pct: -0.5, ai_verdict: null, ai_summary: null, ai_confidence: null },
  ],
}

describe('ResonancePanel 三指标共振', () => {
  afterEach(() => cleanup())
  beforeEach(() => {
    mocks.fetchAPI.mockReset()
    mocks.navigate.mockReset()
  })

  it('渲染共振清单并可点击跳个股页', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<ResonancePanel />)
    expect(await screen.findByText('神剑股份')).toBeTruthy()
    expect(screen.getByText('+3.21%')).toBeTruthy()
    expect(screen.getByText('4.31')).toBeTruthy()
    expect(screen.getByText('+2.50亿')).toBeTruthy()
    fireEvent.click(screen.getByText('神剑股份'))
    expect(mocks.navigate).toHaveBeenCalledWith('/quote/002361')
  })

  it('切换到接近共振会重新请求 only=near', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<ResonancePanel />)
    await waitFor(() => expect(mocks.fetchAPI).toHaveBeenCalled())
    fireEvent.click(screen.getByText('接近共振'))
    await waitFor(() => {
      const last = String(mocks.fetchAPI.mock.calls.at(-1)?.[0] ?? '')
      expect(last).toContain('only=near')
    })
  })

  it('行末展示 AI 判定(含置信度 tooltip), 未生成显示占位', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<ResonancePanel />)
    const tag = await screen.findByText('强共振')
    expect(tag.getAttribute('title')).toContain('置信 91%')
    expect(tag.getAttribute('title')).toContain('三项齐备')
    expect(screen.getAllByText('--').length).toBe(1) // 第二行 AI 未生成
  })

  it('空清单展示占位文案', async () => {
    mocks.fetchAPI.mockResolvedValue({ trade_date: '20260911', count: 0, items: [] })
    render(<ResonancePanel />)
    expect(await screen.findByText(/今日无三指标共振标的/)).toBeTruthy()
  })
})
