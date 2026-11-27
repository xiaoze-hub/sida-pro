// @vitest-environment jsdom
/**
 * 唤醒胶囊（设计稿 §8.7 / §12.3）：
 *  - 点击派发 `sida-open-chat`，detail.pageContext 必须含「数智参谋快照」与 as_of；
 *  - 收起写 localStorage（按标的），并且**仍可恢复**（否则误点一次就永久失去入口）。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ getCopilotBrief: vi.fn() }))
vi.mock('../../src/lib/copilot/api', () => ({
  getCopilotMetrics: vi.fn(async () => ({ days: 7, runs: 0, ok_rate: null, avg_tools: null, p95_elapsed_ms: null, by_recipe: [] })),

  getPendingActions: vi.fn(async () => []),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),

  getCopilotBrief: (...a: unknown[]) => mocks.getCopilotBrief(...a),
  getCopilotPresets: vi.fn().mockResolvedValue([]),
}))

import { CopilotCapsule } from '../../src/components/copilot/CopilotCapsule'
import type { CopilotBrief } from '../../src/lib/copilot/types'

const BRIEF: CopilotBrief = {
  symbol: '600111',
  market: 'CN',
  name: '北方稀土',
  as_of: '2026-10-18T10:31:00+08:00',
  trade_date: '20261018',
  session: { phase: 'intraday', is_trading: true, stale: false, age_s: 3, trade_date: '20261018' },
  quote: { available: true, close: 35.32, change_pct: 1.2, prev_close: 34.9, open: null, high: null, low: null, source: 'tq', as_of: null, degraded_reason: null },
  evidence: {
    chips: { available: true, caliber: 'CYQ', as_of: null, degraded_reason: null, gated: false, main_cost: 35.2, cost_70_low: 33.44, cost_70_high: 36.96, profit_ratio: 91, peak_price: null, peak_ratio: null, cost_90_low: null, cost_90_high: null, profit_state: null, concentration: null, pattern: null, status_label: null, signal_level: null, departure_pct: null, lookback_days: null },
    resonance: { available: true, caliber: 'TQ', as_of: null, degraded_reason: null, gated: false, resonance_state: '⭐ 共振向好', activity: 3.2, gs_zone: null, gs_signal: null, gs_signal_date: null, gs_pending: null, activity_level: null, activity_above_strong: null, activity_streak_days: null, activity_ma5: null, l2_net_wan: null, l2_direction: null, l2_cancel_rate: null, l2_cancel_bias: null, resonance_action: null, resonance_warning: null },
    microstructure: { available: true, caliber: 'TQ撤单', as_of: null, degraded_reason: null, gated: false, b_cancel_ratio: 0.42, s_cancel_ratio: 0.31, session_fallback: false, cancel_bias: null, activity_score: null, intent_tag: null, intent_desc: null, warning_level: null, volume_ratio: null, turnover_rate: null },
    wyckoff: { available: true, caliber: '日K', as_of: null, degraded_reason: null, gated: false, recent: [], stage: null, stage_label: null, effort_result: null, markers_count: null, note: null },
    auction: { available: false, caliber: '竞价', as_of: null, degraded_reason: null, gated: false, verdict_tag: null, verdict_level: null, verdict_action: null, gap_pct: null, open_amount: null, volume_ratio: null, withdraw_rate: null, open_zt_buy: null },
    demon: { available: false, caliber: '妖股', as_of: null, degraded_reason: null, gated: false, total: null, grade: null, max_streak: null, n_events: null, n_sealed: null, participation: null, dims: {}, flags: [], in_top50: null, rank: null },
    theme: { available: false, caliber: '题材', as_of: null, degraded_reason: null, gated: false, group: null, rank: null, limit_up_count: null, leader: null, is_top5: null },
  },
  verdict: {
    intent_label: '低位单峰 · 筹码锁定良好',
    intent_tone: 'opportunity',
    confidence: 0.72,
    evidence_used: ['chips', 'resonance', 'microstructure'],
    evidence_missing: ['auction', 'demon', 'theme'],
    narrative: '底仓成本 35.20 元。',
    defense_lines: [],
    action_plans: [],
    risk_flags: [],
    caliber_notes: ['自算CYQ'],
    disclaimer: '以上为盘口与筹码数据的客观推演，不构成投资建议。',
    rule_id: 'R6',
  },
  cache: { hit: false, ttl_s: 60, key: 'k' },
  version: '1',
}

beforeEach(() => {
  localStorage.clear()
  mocks.getCopilotBrief.mockResolvedValue(BRIEF)
})
afterEach(cleanup)

describe('CopilotCapsule', () => {
  it('紧凑工具栏不改写旧折叠状态，并直接用当前标的打开分析', () => {
    const key = 'copilot_capsule_hidden:600111'
    localStorage.setItem(key, '1')
    const onOpen = vi.fn()
    window.addEventListener('sida-open-chat', onOpen)
    try {
      const view = render(<CopilotCapsule symbol="600111" presentation="inline" compact toolbarBrief={BRIEF} />)
      const button = screen.getByRole('button', { name: '打开分析' })
      const callsBefore = mocks.getCopilotBrief.mock.calls.length
      expect(button.getAttribute('title')).toContain('打开盘口参谋')
      expect(button.classList.contains('fixed')).toBe(false)
      fireEvent.click(button)
      expect(onOpen).toHaveBeenCalledTimes(1)
      expect(onOpen.mock.calls[0][0].detail).toMatchObject({
        symbol: '600111', pageContext: expect.stringContaining(BRIEF.as_of),
      })
      expect(localStorage.getItem(key)).toBe('1')
      view.rerender(<CopilotCapsule symbol="600519" presentation="inline" compact toolbarBrief={BRIEF} />)
      fireEvent.click(screen.getByRole('button', { name: '打开分析' }))
      expect(onOpen.mock.calls[1][0].detail).toMatchObject({
        symbol: '600519', pageContext: expect.stringContaining('尚无参谋快照'),
      })
      expect(onOpen.mock.calls[1][0].detail.pageContext).not.toContain(BRIEF.as_of)
      expect(mocks.getCopilotBrief.mock.calls).toHaveLength(callsBefore)
    } finally {
      window.removeEventListener('sida-open-chat', onOpen)
    }
  })

  it('内联入口忽略旧悬浮位置，收起与恢复始终在原工具栏', async () => {
    localStorage.setItem('copilot_capsule_position', JSON.stringify({ x: 400, y: 200 }))
    render(<CopilotCapsule symbol="600111" presentation="inline" />)
    const toolbar = screen.getByTestId('copilot-inline-toolbar')
    expect(toolbar.classList.contains('fixed')).toBe(false)
    expect(toolbar.style.left).toBe('')
    expect(toolbar.style.boxShadow).toBe('')
    expect(screen.queryByTestId('copilot-capsule')).toBeNull()
    fireEvent.click(screen.getByTestId('copilot-capsule-hide'))
    expect(toolbar.contains(screen.getByTestId('copilot-capsule-restore'))).toBe(true)
    fireEvent.click(screen.getByTestId('copilot-capsule-restore'))
    expect(toolbar.contains(screen.getByTestId('copilot-capsule-open'))).toBe(true)
  })

  it('点击后派发 sida-open-chat 且携带快照上下文', async () => {
    const events: CustomEvent[] = []
    const onOpen = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('sida-open-chat', onOpen)

    render(<CopilotCapsule symbol="600111" />)
    await waitFor(() => expect(screen.getByText(/低位单峰/)).toBeTruthy())

    fireEvent.click(screen.getByTestId('copilot-capsule-open'))
    expect(events).toHaveLength(1)
    const detail = events[0].detail as { symbol: string; pageContext: string }
    expect(detail.symbol).toBe('600111')
    expect(detail.pageContext).toContain('数智参谋快照')
    expect(detail.pageContext).toContain(BRIEF.as_of)
    expect(detail.pageContext).toContain('底仓成本')

    window.removeEventListener('sida-open-chat', onOpen)
  })

  it('收起写入 localStorage，回访仍是收起态，但可恢复', async () => {
    const { unmount } = render(<CopilotCapsule symbol="600111" />)
    await waitFor(() => expect(screen.getByTestId('copilot-capsule')).toBeTruthy())

    fireEvent.click(screen.getByTestId('copilot-capsule-hide'))
    expect(localStorage.getItem('copilot_capsule_hidden:600111')).toBe('1')
    expect(screen.queryByTestId('copilot-capsule')).toBeNull()

    // 恢复入口必须存在（否则误点一次永久失去入口）
    fireEvent.click(screen.getByTestId('copilot-capsule-restore'))
    expect(localStorage.getItem('copilot_capsule_hidden:600111')).toBeNull()
    expect(screen.getByTestId('copilot-capsule')).toBeTruthy()

    unmount()
  })

  it('无快照时也能唤醒（不阻塞聊天入口）', async () => {
    mocks.getCopilotBrief.mockRejectedValue(new Error('404'))
    const events: CustomEvent[] = []
    const onOpen = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('sida-open-chat', onOpen)

    render(<CopilotCapsule symbol="600111" />)
    await waitFor(() => expect(screen.getByText(/盘口参谋/)).toBeTruthy())
    fireEvent.click(screen.getByTestId('copilot-capsule-open'))
    expect((events[0].detail as { pageContext: string }).pageContext).toContain('尚无参谋快照')

    window.removeEventListener('sida-open-chat', onOpen)
  })

  it('拖拽后吸附左边缘并持久化位置；松手那一下不误触唤醒', async () => {
    const events: CustomEvent[] = []
    const onOpen = (e: Event) => events.push(e as CustomEvent)
    window.addEventListener('sida-open-chat', onOpen)

    render(<CopilotCapsule symbol="600111" />)
    const capsule = await screen.findByTestId('copilot-capsule')

    fireEvent.pointerDown(capsule, { button: 0, pointerId: 1, clientX: 20, clientY: 20 })
    fireEvent.pointerMove(capsule, { pointerId: 1, clientX: 40, clientY: 120 })
    fireEvent.pointerUp(capsule, { pointerId: 1, clientX: 40, clientY: 120 })

    // 吸附: 40px 已进入左吸附区(12 + 72) → 贴左边缘 12px；y 保持自由(120 - 抓取偏移 20)
    const stored = JSON.parse(localStorage.getItem('copilot_capsule_position') || '{}')
    expect(stored.x).toBe(12)
    expect(stored.y).toBe(100)
    expect((capsule as HTMLElement).style.left).toBe('12px')

    // 拖拽结束的 click 必须被吞掉（否则松手就弹聊天面板）
    fireEvent.click(screen.getByTestId('copilot-capsule-open'))
    expect(events).toHaveLength(0)

    // 抑制只作用于那一下：随后的正常点击仍能唤醒
    await new Promise((r) => setTimeout(r, 20))
    fireEvent.click(screen.getByTestId('copilot-capsule-open'))
    expect(events).toHaveLength(1)

    window.removeEventListener('sida-open-chat', onOpen)
  })
})
