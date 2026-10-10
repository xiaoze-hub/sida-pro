// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * P0-3 决策合成接 UI 的契约: `DecisionVerdictCard` 消费 `GET /api/decision/{symbol}`
 * (三信号 → 动手/看看/别碰 + 一行理由)。守五件事:
 *
 * ① **三态徽标**: 后端三个 verdict 各出对应文案的徽标(且带语义色 class);
 * ② **理由一行**: `reason` 原文上屏(不截断、不改写);
 * ③ **换标的竞态**: 旧标的响应迟到落地时**一律丢弃**(只认最新号), 不把上一只票的结论画到新标的名下;
 * ④ **失败显式空态**: 取数 reject ⇒ 显式失败态(不猜方向、不落默认 verdict、不出徽标);
 * ⑤ **无关重渲染不重发**: 同 `symbol`/`market` 的重渲染不重放请求(取数只挂在 [symbol, market])。
 *
 * 另守两条诚实边界: verdict 缺失/表外 ⇒ 显式无数据(不冒充方向); verdict 有而 reason 缺 ⇒ 显式
 * 「理由无数据」(不编造)。
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

describe('DecisionVerdictCard · 三态徽标 + 一行理由', () => {
  it.each([
    ['动手', 'text-gs-go'],
    ['看看', 'text-role-watch'],
    ['别碰', 'text-gs-stop'],
  ])('verdict=%s ⇒ 徽标文案与语义色正确, 理由原文上屏', async (verdict, tone) => {
    mocks.decision.mockResolvedValue({
      symbol: '002636',
      verdict,
      reason: `${verdict}: 趋势G、活跃度26.68、主力流入2.30亿`,
      phase: '向好',
      row: 3,
    })
    render(<DecisionVerdictCard symbol="002636" market="CN" />)

    const badge = await screen.findByTestId('decision-badge')
    expect(badge.textContent).toBe(verdict)
    expect(badge.className).toContain(tone)
    expect(screen.getByTestId('decision-reason').textContent).toBe(
      `${verdict}: 趋势G、活跃度26.68、主力流入2.30亿`,
    )
    // 加载态退场, 失败态不出现
    expect(screen.queryByTestId('decision-loading')).toBeNull()
    expect(screen.queryByTestId('decision-error')).toBeNull()
    // 参数: (symbol, market) 原样透传
    expect(mocks.decision).toHaveBeenCalledWith('002636', 'CN')
  })

  it('verdict 缺失/表外 ⇒ 显式无数据, 不出徽标(不猜方向)', async () => {
    mocks.decision.mockResolvedValue({ symbol: '002636', reason: 'whatever' })
    render(<DecisionVerdictCard symbol="002636" market="CN" />)
    expect(await screen.findByTestId('decision-empty')).toBeTruthy()
    expect(screen.queryByTestId('decision-badge')).toBeNull()
  })

  it('verdict 有而 reason 缺 ⇒ 显式「理由无数据」(不编造理由)', async () => {
    mocks.decision.mockResolvedValue({ symbol: '002636', verdict: '动手' })
    render(<DecisionVerdictCard symbol="002636" market="CN" />)
    expect((await screen.findByTestId('decision-badge')).textContent).toBe('动手')
    expect(screen.getByTestId('decision-reason-missing')).toBeTruthy()
    expect(screen.queryByTestId('decision-reason')).toBeNull()
  })
})

describe('DecisionVerdictCard · 竞态/失败/去重', () => {
  it('换标的竞态: 旧标的迟到响应被丢弃, 屏上只留新标的结论', async () => {
    let resolveOld: (v: unknown) => void = () => {}
    const oldPending = new Promise((r) => {
      resolveOld = r
    })
    mocks.decision.mockImplementation((sym: string) =>
      sym === 'AAA' ? oldPending : Promise.resolve({ symbol: 'BBB', verdict: '动手', reason: '动手: 三信号共振' }),
    )

    const { rerender } = render(<DecisionVerdictCard symbol="AAA" market="CN" />)
    // 旧标的请求悬挂在途 → 加载态
    expect(screen.getByTestId('decision-loading')).toBeTruthy()
    expect(mocks.decision).toHaveBeenCalledWith('AAA', 'CN')

    // 换标的 ⇒ 新请求落地
    rerender(<DecisionVerdictCard symbol="BBB" market="CN" />)
    expect((await screen.findByTestId('decision-badge')).textContent).toBe('动手')

    // 旧标的响应迟到 ⇒ 必须被丢弃(不覆盖新标的)
    await act(async () => {
      resolveOld({ symbol: 'AAA', verdict: '别碰', reason: '别碰: 趋势走坏' })
      await Promise.resolve()
    })
    expect(screen.getByTestId('decision-badge').textContent).toBe('动手')
    expect(screen.queryByText('别碰: 趋势走坏')).toBeNull()
    expect(mocks.decision).toHaveBeenCalledWith('BBB', 'CN')
  })

  it('取数失败 ⇒ 显式失败态(不猜方向、不出徽标)', async () => {
    mocks.decision.mockRejectedValue(new Error('HTTP 500'))
    render(<DecisionVerdictCard symbol="002636" market="CN" />)
    const err = await screen.findByTestId('decision-error')
    expect(err.textContent).toContain('决策合成取数失败')
    expect(err.textContent).toContain('HTTP 500')
    expect(screen.queryByTestId('decision-badge')).toBeNull()
    expect(screen.queryByTestId('decision-loading')).toBeNull()
  })

  it('同 symbol/market 的无关重渲染不重发请求(一次取数)', async () => {
    mocks.decision.mockResolvedValue({ symbol: '002636', verdict: '看看', reason: '看看: 观望' })
    const { rerender } = render(<DecisionVerdictCard symbol="002636" market="CN" />)
    await screen.findByTestId('decision-badge')
    rerender(<DecisionVerdictCard symbol="002636" market="CN" />)
    rerender(<DecisionVerdictCard symbol="002636" market="CN" />)
    await waitFor(() => expect(screen.getByTestId('decision-badge').textContent).toBe('看看'))
    expect(mocks.decision).toHaveBeenCalledTimes(1)
  })
})

describe('DecisionVerdictCard · 证据化(2026-10-10)', () => {
  it('响应带证据链/相似情形 ⇒ 追加渲染(徽标与理由零改动)', async () => {
    mocks.decision.mockResolvedValue({
      symbol: '002636',
      verdict: '动手',
      reason: '动手: 趋势G、活跃度26.68、主力流入2.30亿',
      phase: '向好',
      evidence: {
        triggers: ['趋势 G信号', '活跃度 26.68', '主力净流入 2.30亿'],
        as_of: '2026-10-10T07:00:00',
        as_of_is_today: true,
        as_of_note: '数据时点: 20261010(今日)',
        invalidation: ['若趋势转弱 / 资金转净流出, 则『动手』作废'],
        invalidation_defaulted: true,
      },
      similar: { n: 40, up: 22, insufficient: false, sentence: '历史上 40 次相似情形, 22 次后续上涨' },
    })
    render(<DecisionVerdictCard symbol="002636" market="CN" />)
    expect((await screen.findByTestId('decision-badge')).textContent).toBe('动手')
    const ev = screen.getByTestId('decision-evidence')
    expect(ev).toBeTruthy()
    expect(screen.getByTestId('decision-evidence-triggers').textContent).toContain('活跃度 26.68')
    expect(screen.getByTestId('decision-evidence-asof').textContent).toContain('今日')
    expect(screen.getByTestId('decision-evidence-invalidation').textContent).toContain('作废')
    expect(screen.getByTestId('decision-evidence-similar').textContent).toContain('22 次后续上涨')
    // 徽标/理由既有行为逐字不变
    expect(screen.getByTestId('decision-reason').textContent).toBe('动手: 趋势G、活跃度26.68、主力流入2.30亿')
  })

  it('响应无证据字段(旧后端) ⇒ 不渲染证据块, 逐字向后兼容', async () => {
    mocks.decision.mockResolvedValue({ symbol: '002636', verdict: '看看', reason: '看看: 观望' })
    render(<DecisionVerdictCard symbol="002636" market="CN" />)
    await screen.findByTestId('decision-badge')
    expect(screen.queryByTestId('decision-evidence')).toBeNull()
  })
})
