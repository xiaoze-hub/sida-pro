// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * P1-2(个性化) + P2-7(跨市场) 前端契约: `DecisionVerdictCard` 对后端新增字段的**显式**渲染,
 * 且旧响应(无这些字段)行为与旧版逐字一致(向后兼容)。
 *
 * 真数据纪律: mock 的是**网络层**(`@panwatch/api` 的 `insightApi.decision`), 组件走真实代码。
 */
const mocks = vi.hoisted(() => ({ decision: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  insightApi: {
    decision: (...a: unknown[]) => mocks.decision(...a),
  },
}))

import DecisionVerdictCard from '@/pages/workbench/DecisionVerdictCard'

beforeEach(() => {
  mocks.decision.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('DecisionVerdictCard · 个性化/跨市场新增字段', () => {
  it('personalization_note ⇒ 个性化注记显式上屏, 徽标仍为 verdict', async () => {
    mocks.decision.mockResolvedValue({
      symbol: '600519',
      verdict: '看看',
      reason: '看看: 趋势G区间、活跃度5.00、主力流入1.00亿',
      personalized: true,
      personalization_note: '个性化(保守型)：『动手』收紧至三指标首次/再次共振(状态表行1/2), 当前行3(向好)收敛为『看看』',
      risk_profile: 'conservative',
    })
    render(<DecisionVerdictCard symbol="600519" market="CN" />)

    const note = await screen.findByTestId('decision-personalization')
    expect(note.textContent).toContain('保守型')
    expect(note.textContent).toContain('个性化调整：')
    expect((await screen.findByTestId('decision-badge')).textContent).toBe('看看')
  })

  it('position.note ⇒ 持仓参考行升屏(不含空态占位)', async () => {
    mocks.decision.mockResolvedValue({
      symbol: '600519',
      verdict: '动手',
      reason: '动手: 三指标首次共振',
      position: { cost_price: 10, quantity: 1000, last_close: 11, pnl_pct: 10, note: '已持仓：持仓成本 10.00 / 浮盈 +10.00%' },
    })
    render(<DecisionVerdictCard symbol="600519" market="CN" />)

    const pos = await screen.findByTestId('decision-position')
    expect(pos.textContent).toBe('已持仓：持仓成本 10.00 / 浮盈 +10.00%')
    expect((await screen.findByTestId('decision-badge')).textContent).toBe('动手')
  })

  it('basis=two-dimension + fund_note ⇒ 显式「资金维无数据(非CN)」标注', async () => {
    mocks.decision.mockResolvedValue({
      symbol: '00700',
      verdict: '动手',
      reason: '动手: 趋势G信号、活跃度5.00；资金维无数据(非CN), 仅趋势×活跃度双维',
      basis: 'two-dimension',
      fund_note: '资金维无数据(非CN)',
      parts: { trend: 'G信号', activity: 5, fund_net: null },
    })
    render(<DecisionVerdictCard symbol="00700" market="HK" />)

    const miss = await screen.findByTestId('decision-fund-missing')
    expect(miss.textContent).toContain('资金维无数据(非CN)')
    expect(miss.textContent).toContain('仅趋势 × 活跃度双维')
    expect(mocks.decision).toHaveBeenCalledWith('00700', 'HK')
  })

  it('旧响应(无新字段) ⇒ 不渲染新行, 三态/理由与旧版一致(向后兼容)', async () => {
    mocks.decision.mockResolvedValue({
      symbol: '002636',
      verdict: '别碰',
      reason: '别碰: 趋势S信号、活跃度1.00、主力流出0.50亿',
      phase: '走坏',
      row: 7,
    })
    render(<DecisionVerdictCard symbol="002636" market="CN" />)

    expect((await screen.findByTestId('decision-badge')).textContent).toBe('别碰')
    expect(screen.getByTestId('decision-reason').textContent).toBe(
      '别碰: 趋势S信号、活跃度1.00、主力流出0.50亿',
    )
    expect(screen.queryByTestId('decision-personalization')).toBeNull()
    expect(screen.queryByTestId('decision-position')).toBeNull()
    expect(screen.queryByTestId('decision-fund-missing')).toBeNull()
  })
})
