// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

/**
 * DecisionPioneerCard「主力净流入(L2·TQ)」非交易时段回退的**前端诚实性**守线(2026-10-11)。
 *
 * 背景(生产截图 600111 周末): 卡片走另一条取数路, 非交易时段显示"0万 平衡 逐笔0笔"。
 * 后端已套同款回退(见 tests/test_decision_l2_fallback.py); 本测试守**前端据 source/as_of 如实标注**:
 *   - source='snapshot'(非实时) → 显式标 "YYYY-MM-DD 收盘"(不让回收退值冒充实时);
 *   - available=false + note → 显式显示后端 note(含"不冒充 0"), 不静默回退成 0/平衡;
 *   - source='live' → 不出"收盘"标注(实时无需标注)。
 */
const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
}))
vi.mock('@panwatch/biz-ui/components/ActivitySparkline', () => ({
  default: () => <div data-testid="activity-sparkline" />,
}))

import DecisionPioneerCard from '@panwatch/biz-ui/components/DecisionPioneerCard'

function _resp(l2: Record<string, unknown>) {
  return {
    symbol: '600111',
    institution_activity: null,
    gs: null,
    l2,
    data_time: '2026-10-11T09:30:00+08:00',
  }
}

describe('DecisionPioneerCard L2 非交易时段回退标注', () => {
  afterEach(() => {
    cleanup()
    mocks.fetchAPI.mockReset()
  })

  it('回退快照 → 显式标 "YYYY-MM-DD 收盘"(0 不冒充实时)', async () => {
    mocks.fetchAPI.mockResolvedValue(
      _resp({
        available: true,
        zjl_hb: -3762.99 * 1e4,
        direction: '净流出',
        l2_tick_num: 120327,
        l2_order_num: 224629,
        source: 'snapshot',
        as_of: '2026-10-09',
        note: '非交易时段·显示 2026-10-09 收盘值',
      }),
    )
    render(<DecisionPioneerCard symbol="600111" market="CN" />)
    await waitFor(() => expect(screen.getByText('2026-10-09 收盘')).toBeTruthy())
    // 快照真实值展示(非 0)
    expect(screen.queryByText(/^0万$/)).toBeNull()
    expect(screen.getByText('逐笔120327笔 · 委托224629笔')).toBeTruthy()
  })

  it('无快照 → available:false + 显后端 note(含"不冒充 0")', async () => {
    mocks.fetchAPI.mockResolvedValue(
      _resp({
        available: false,
        source: 'session_closed',
        as_of: null,
        note: '非交易时段·无收盘快照, 无数据(不冒充 0)',
      }),
    )
    render(<DecisionPioneerCard symbol="600111" market="CN" />)
    await waitFor(() =>
      expect(screen.getByText(/无数据\(.*不冒充 0.*\)/)).toBeTruthy(),
    )
  })

  it('实时(source=live) → 不出"收盘"标注', async () => {
    mocks.fetchAPI.mockResolvedValue(
      _resp({
        available: true,
        zjl_hb: 1234 * 1e4,
        direction: '净流入',
        l2_tick_num: 10,
        l2_order_num: 20,
        source: 'live',
      }),
    )
    render(<DecisionPioneerCard symbol="600111" market="CN" />)
    await waitFor(() => expect(screen.getByText('逐笔10笔 · 委托20笔')).toBeTruthy())
    expect(screen.queryByText(/收盘$/)).toBeNull()
  })
})
