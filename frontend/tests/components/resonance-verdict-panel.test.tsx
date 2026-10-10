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
    // 默认(bare 未传): 自己的「三指标」标题仍在 —— 既有调用点行为不变
    expect(screen.getByText('三指标')).toBeTruthy()
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

  it('bare 时不出自己的「三指标」标题(合并卡合成用), 三灯/AI 按钮仍在', async () => {
    mocks.fetchAPI.mockImplementation(async (url: string) => {
      if (String(url).includes('/analyze/')) return AI_OK
      return RULE
    })
    render(<ResonanceVerdictPanel symbol="300563" bare />)
    await waitFor(() => expect(screen.getByText(/趋势 G区间/)).toBeTruthy())
    expect(screen.queryByText('三指标')).toBeNull()
    expect(screen.getByText('规则: 强')).toBeTruthy()
    expect(screen.getByText('AI 分析')).toBeTruthy()
  })

  it('证据化: 追加渲染证据链/失效条件/置信度校准/相似情形(不改既有判定)', async () => {
    const ai = {
      ...AI_OK,
      evidence: {
        triggers: ['趋势 G区间(G 区向上)', '活跃度 26.68 ≥ 强势线 3.0'],
        as_of: '20260911',
        as_of_is_today: false,
        as_of_note: '数据时点: 20260911(非今日, 基准日滞后)',
        invalidation: ['若 GS 趋势转 S 区则作废'],
        invalidation_defaulted: false,
      },
      confidence_calibration: {
        calibrated: false, value: null, cap: null, n: 0, hit_rate: null,
        note: '未校准(样本不足: n=0 < 30, 不给校准置信度)',
      },
      similar: { n: 0, up: null, insufficient: true, sentence: '历史相似情形样本不足(N=0 < 30), 不给百分比' },
    }
    mocks.fetchAPI.mockImplementation(async (url: string) => {
      if (String(url).includes('/analyze/')) return ai
      return RULE
    })
    render(<ResonanceVerdictPanel symbol="300563" />)
    await waitFor(() => expect(screen.getByText(/趋势 G区间/)).toBeTruthy())
    fireEvent.click(screen.getByText('AI 分析'))
    expect(await screen.findByTestId('resonance-evidence')).toBeTruthy()
    expect(screen.getByTestId('resonance-evidence-triggers').textContent).toContain('活跃度 26.68')
    expect(screen.getByTestId('resonance-evidence-asof').textContent).toContain('非今日')
    expect(screen.getByTestId('resonance-evidence-invalidation').textContent).toContain('转 S 区则作废')
    expect(screen.getByTestId('resonance-evidence-calibration').textContent).toContain('未校准')
    expect(screen.getByTestId('resonance-evidence-similar').textContent).toContain('样本不足')
    // 既有判定与依据仍照常渲染(零改动)
    expect(screen.getByText('强共振')).toBeTruthy()
    expect(screen.getByText(/依据:/)).toBeTruthy()
  })
})
