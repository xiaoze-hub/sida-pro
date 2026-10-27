// @vitest-environment jsdom
/**
 * 多维数智参谋卡（设计稿 §8.4 / §12.3）：
 *  - 三段式结论齐全（意图定性 / 关键防守位 / 操盘预案）+ 合规尾注；
 *  - 权限不足的证据块显示「升级解锁」而不是 `--`（gated ≠ missing）；
 *  - 防守位为空时明说「不给防守线（不猜价位）」；
 *  - 持仓者预案置顶。
 */
import { cleanup, render as rtlRender, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ state: { data: null as unknown, loading: false, error: null as string | null } }))
vi.mock('../../src/hooks/useCopilotBrief', () => ({
  useCopilotBrief: () => ({ ...mocks.state, reload: vi.fn() }),
}))

import { CopilotBriefCard } from '../../src/components/copilot/CopilotBriefCard'

const render = (ui: Parameters<typeof rtlRender>[0]) => rtlRender(<MemoryRouter>{ui}</MemoryRouter>)

const BASE = {
  symbol: '600111',
  market: 'CN',
  name: '北方稀土',
  as_of: '2026-10-18T10:31:00+08:00',
  trade_date: '20261018',
  session: { phase: 'intraday', is_trading: true, stale: false, age_s: 2, trade_date: '20261018' },
  quote: { available: true, close: 35.32, change_pct: 1.2, prev_close: 34.9, open: null, high: null, low: null, source: 'tq', as_of: null, degraded_reason: null },
  evidence: {
    chips: { available: true, caliber: 'CYQ', as_of: null, degraded_reason: null, gated: false },
    resonance: { available: false, caliber: 'TQ', as_of: null, degraded_reason: '需要「数智决策」权限', gated: true },
    microstructure: { available: true, caliber: 'TQ撤单', as_of: null, degraded_reason: null, gated: false },
    wyckoff: { available: true, caliber: '日K', as_of: null, degraded_reason: null, gated: false },
    auction: { available: false, caliber: '竞价', as_of: null, degraded_reason: '今日无竞价异动记录', gated: false },
    demon: { available: false, caliber: '妖股', as_of: null, degraded_reason: null, gated: false },
    theme: { available: false, caliber: '题材', as_of: null, degraded_reason: null, gated: false },
  },
  verdict: {
    intent_label: '托单诱多 · 高位兑现压力',
    intent_tone: 'risk',
    confidence: 0.66,
    evidence_used: ['chips', 'microstructure'],
    evidence_missing: ['resonance', 'auction'],
    narrative: '底仓成本 35.20 元，获利盘 91%。',
    defense_lines: [
      { slot: 'first', label: '第一防守', price: 34.8, basis: '今日探底', source: 'intraday_low', side: 'support', distance_pct: -1.47 },
      { slot: 'core', label: '核心生命线', price: 33.44, basis: '70%筹码下沿', source: 'cost_70_low', side: 'support', distance_pct: -5.32 },
      { slot: 'cap', label: '强压制', price: 36.96, basis: '70%筹码上沿', source: 'cost_70_high', side: 'resistance', distance_pct: 4.64 },
    ],
    action_plans: [
      { audience: 'empty', action: 'avoid', title: '不参与', text: '等核心生命线被收复再看。', triggers: [{ price: 33.44, kind: 'invalidate', note: '收复确认' }] },
      { audience: 'holder', action: 'reduce', title: '减仓防守', text: '现价附近分批降低仓位。', triggers: [{ price: 34.8, kind: 'defend', note: '跌破先减半' }] },
    ],
    risk_flags: ['high_profit_ratio'],
    caliber_notes: ['自算CYQ·120日', 'TQ撤单'],
    disclaimer: '以上为盘口与筹码数据的客观推演，不构成投资建议。',
    rule_id: 'R1',
  },
  cache: { hit: true, ttl_s: 60, key: 'k' },
  version: '1',
}

afterEach(cleanup)

describe('CopilotBriefCard', () => {
  it('渲染三段式结论 + 合规尾注', () => {
    mocks.state = { data: BASE, loading: false, error: null }
    render(<CopilotBriefCard symbol="600111" />)
    expect(screen.getByTestId('copilot-card')).toBeTruthy()
    expect(screen.getByText('托单诱多 · 高位兑现压力')).toBeTruthy()
    expect(screen.getByText('关键防守位')).toBeTruthy()
    expect(screen.getByText('操盘预案')).toBeTruthy()
    expect(screen.getByTestId('copilot-defense-first')).toBeTruthy()
    expect(screen.getByTestId('copilot-defense-cap')).toBeTruthy()
    expect(screen.getByText(/不构成投资建议/)).toBeTruthy()
    expect(screen.getByText(/\[R1\]/)).toBeTruthy()
  })

  it('权限不足的证据块显示「升级解锁」，不是 --', () => {
    mocks.state = { data: BASE, loading: false, error: null }
    render(<CopilotBriefCard symbol="600111" />)
    const gated = document.querySelector('[data-evidence="resonance"]')
    expect(gated?.getAttribute('data-state')).toBe('gated')
    expect(gated?.textContent).toContain('升级解锁')
    const missing = document.querySelector('[data-evidence="auction"]')
    expect(missing?.getAttribute('data-state')).toBe('missing')
  })

  it('无防守位时明说不猜价位', () => {
    const data = { ...BASE, verdict: { ...BASE.verdict, defense_lines: [] } }
    mocks.state = { data, loading: false, error: null }
    render(<CopilotBriefCard symbol="600111" />)
    expect(screen.getByText(/不猜价位/)).toBeTruthy()
  })

  it('有持仓时「持仓者」预案置顶', () => {
    mocks.state = { data: BASE, loading: false, error: null }
    render(<CopilotBriefCard symbol="600111" hasPosition />)
    const holder = screen.getByTestId('copilot-plan-holder')
    const empty = screen.getByTestId('copilot-plan-empty')
    expect(holder.compareDocumentPosition(empty) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(holder.textContent).toContain('你的位置')
  })

  it('接口失败时只陈述错误原文，不给假结论', () => {
    mocks.state = { data: null, loading: false, error: '404 未知标的' }
    render(<CopilotBriefCard symbol="600111" />)
    expect(screen.getByTestId('copilot-card-error')).toBeTruthy()
    expect(screen.getByText('404 未知标的')).toBeTruthy()
    expect(screen.queryByTestId('copilot-card')).toBeNull()
  })
})
