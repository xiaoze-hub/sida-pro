// @vitest-environment jsdom
//
// 决策先锋辅助指标数据面板节(2026-10-10 P3 UI 集成)渲染回归:
// 挂在「数智决策」合并卡(DecisionCard)**尾部一节**, 展示三线现值 + 买卖点枚举 + 校准标注。
// 子卡(DecisionPioneerCard/ResonanceVerdictPanel)mock —— 本测试守"追加节"接线与读数, 不测子卡内部。
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const H = vi.hoisted(() => ({
  calls: [] as string[],
  trendLine: {} as Record<string, unknown>,
  niuxiong: {} as Record<string, unknown>,
}))

vi.mock('@panwatch/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string) => {
    const u = String(url)
    H.calls.push(u)
    if (u.includes('/indicators/trend-line/')) return Promise.resolve(H.trendLine)
    if (u.includes('/indicators/niuxiong/')) return Promise.resolve(H.niuxiong)
    return Promise.resolve({})
  }),
}))

vi.mock('@panwatch/biz-ui/components/DecisionPioneerCard', () => ({
  default: () => <div data-testid="dpc">dpc</div>,
}))
vi.mock('@panwatch/biz-ui/components/ResonanceVerdictPanel', () => ({
  default: () => <div data-testid="rvp">rvp</div>,
}))

import DecisionCard from '@panwatch/biz-ui/components/workbench/DecisionCard'

beforeEach(() => {
  H.calls.length = 0
  H.trendLine = {
    available: true,
    lines: { red: 11, yellow: 10.5, green: 9.8 },
    band: { state: '多方带' },
    trend: '升势',
    buy_points: [{ signal: 'buy', rule: 'pullback_green_yang', trigger: '回踩绿线收阳', time: '2026-03-07' }],
    sell_points: [{ signal: 'sell', rule: 'rebound_green_fail', trigger: '跌势反弹绿线无力突破', time: '2026-03-08' }],
    params: { red_period: 10, yellow_period: 20, green_period: 60 },
    calibration: '逆向近似待校准(EMA 近似, 参数见 thresholds 配置层)',
  }
  H.niuxiong = {
    available: true,
    lines: { bull: 12, horse: 10, trade: 9 },
    signal: 'B',
    color: 'red',
    state: '牛线上方',
    cross: { type: 'golden', direction: 'B', bars_ago: 2, time: '2026-03-06' },
    params: { bull_period: 20, horse_period: 5, trade_period: 30 },
    calibration: '买卖线口径逆向近似待校准',
  }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('数智决策数据面板 · 辅助指标追加节', () => {
  it('DecisionCard 尾部渲染辅助指标节: 三线现值 + 买卖点枚举 + 校准标注', async () => {
    render(<DecisionCard symbol="002636" market="CN" />)

    const section = await screen.findByTestId('pioneer-indicators-section')
    await waitFor(() => expect(section.textContent).toContain('操盘线'))

    // 取数接线: 两个辅助指标端点各取一次(带 market)
    expect(H.calls.some((u) => u.includes('/indicators/trend-line/002636?market=CN'))).toBe(true)
    expect(H.calls.some((u) => u.includes('/indicators/niuxiong/002636?market=CN'))).toBe(true)

    const text = section.textContent || ''
    // ① 三线现值
    expect(text).toContain('11.00') // 操盘线红
    expect(text).toContain('9.80') // 操盘线绿
    expect(text).toContain('升势')
    expect(text).toContain('多方带')
    expect(text).toContain('牛熊线')
    expect(text).toContain('金叉 B')
    // ② 买卖点枚举(客观规则文案, 买/卖)
    expect(text).toContain('买·回踩绿线收阳')
    expect(text).toContain('卖·跌势反弹绿线无力突破')
    // ③ 校准标注 + 客观标注免责
    expect(screen.getByTestId('pioneer-calibration').textContent).toContain('逆向近似待校准')
    expect(text).toContain('客观标注 · 非投资建议')
  })

  it('无数据(available=false)→ 原样展示后端 note, 不编造', async () => {
    H.trendLine = { available: false, note: '日K不足或无数据, 无法计算趋势操盘线' }
    H.niuxiong = { available: false, note: '日K不足或无数据, 无法计算牛熊线' }
    render(<DecisionCard symbol="002636" market="CN" />)

    const section = await screen.findByTestId('pioneer-indicators-section')
    await waitFor(() => expect(section.textContent).toContain('日K不足'))
    expect(section.textContent).toContain('无法计算趋势操盘线')
    expect(section.textContent).toContain('无法计算牛熊线')
  })

  it('仅 CN 标的取数(非 CN 不发请求)', async () => {
    render(<DecisionCard symbol="AAPL" market="US" />)
    expect(screen.getByTestId('pioneer-indicators-section')).toBeTruthy()
    await new Promise((r) => setTimeout(r, 50))
    expect(H.calls.some((u) => u.includes('/indicators/'))).toBe(false)
  })
})
