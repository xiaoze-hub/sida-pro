// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// 决策先锋共振判定面板 (2026-09-11): 规则三灯(GET /resonance/symbol) + AI 判定(POST /resonance/analyze)。
const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))

import ResonanceVerdictPanel from '@panwatch/biz-ui/components/ResonanceVerdictPanel'

const RULE = {
  symbol: '300563',
  available: true,
  trade_date: '20260911',
  trend: 'G区间',
  activity: 26.68,
  level: '大牛',
  fund_net: 2.3e8,
  level3: '强',
  hits: [true, true, true],
}

const AI_OK = {
  symbol: '300563',
  available: true,
  rule: RULE,
  ai: {
    resonance: '强共振',
    confidence: 0.8,
    summary: '三指标对齐',
    reasons: ['趋势G区', '活跃度26.68'],
    risks: ['涨幅已大'],
    watch: ['次日承接'],
    missing: [],
  },
}

describe('ResonanceVerdictPanel 三灯 + AI 判定', () => {
  afterEach(() => cleanup())
  beforeEach(() => mocks.fetchAPI.mockReset())

  it('渲染三灯与规则结论, 点 AI 分析展示结构化判定', async () => {
    mocks.fetchAPI.mockImplementation(async (url: string) => {
      if (String(url).includes('/analyze/')) return AI_OK
      return RULE
    })
    render(<ResonanceVerdictPanel symbol="300563" />)
    await waitFor(() => expect(screen.getByText(/趋势 G区间/)).toBeTruthy())
    expect(screen.getByText(/强度 26.68\(大牛\)/)).toBeTruthy()
    expect(screen.getByText(/资金 2.30亿/)).toBeTruthy()
    expect(screen.getByText('规则: 强')).toBeTruthy()

    fireEvent.click(screen.getByText('AI 分析'))
    expect(await screen.findByText('强共振')).toBeTruthy()
    expect(screen.getByText(/依据:/)).toBeTruthy()
    expect(screen.getByText(/风险:/)).toBeTruthy()
  })

  it('AI 不可用时如实提示并保留三灯', async () => {
    mocks.fetchAPI.mockImplementation(async (url: string) => {
      if (String(url).includes('/analyze/')) return { symbol: '300563', available: false, reason: 'AI 不可用: timeout', ai: null }
      return RULE
    })
    render(<ResonanceVerdictPanel symbol="300563" />)
    await waitFor(() => expect(screen.getByText('规则: 强')).toBeTruthy())
    fireEvent.click(screen.getByText('AI 分析'))
    expect(await screen.findByText(/AI 不可用: timeout/)).toBeTruthy()
    expect(screen.getByText(/趋势 G区间/)).toBeTruthy()
  })

  it('规则数据缺失时禁用 AI 按钮并标注', async () => {
    mocks.fetchAPI.mockImplementation(async () => ({ symbol: '300563', available: false, reason: '无日线数据' }))
    render(<ResonanceVerdictPanel symbol="300563" />)
    await waitFor(() => expect(screen.getByText('数据缺失')).toBeTruthy())
    const btn = screen.getByText('AI 分析').closest('button') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })
})
