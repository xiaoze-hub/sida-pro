// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * DecisionCard 合并卡的**视觉契约**(复审修复: 嵌套卡壳 + 重复/倒挂标题)。
 *
 * 与 decision-card.test.tsx(mock 两个子组件, 守去重/透传)不同, 本文件渲染**真实子组件**,
 * 直接量 DOM 里"卡壳"(圆角 + 四边完整 token 边框)与标题层级的数量 —— 这正是复审 finding
 * 的判定面: 合并后必须**只有一层边框**、**只有一个顶层标题**。
 *
 * 同时回归"默认路径不变": 不传 `bare` 时 `DecisionPioneerCard` 仍出自己的卡壳与
 * 「🧭 数智决策三指标」标题(旧调用点 DarkFlowCards / 旧行情页行为逐字不变)。
 */
const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn(), setOption: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('@panwatch/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, on: vi.fn(), off: vi.fn(), resize: vi.fn() } },
  }),
}))

import DecisionCard from '@panwatch/biz-ui/components/workbench/DecisionCard'
import DecisionPioneerCard from '@panwatch/biz-ui/components/DecisionPioneerCard'

const PIONEER = {
  symbol: '002636',
  institution_activity: { activity: 26.68, level: '大牛', life_line: 1.56, strong_line: 3, bull_line: 6, streak_days: 3, ma5: 20.1 },
  gs: { signal: 'G', state: 'G区', bb0: 19.8, a0: 20.5 },
  l2: { available: true, zjl_hb: 2.3e8, direction: '净流入', l2_tick_num: 120, l2_order_num: 88 },
  data_time: '2026-09-13 14:30',
}

const RULE = {
  symbol: '002636',
  available: true,
  trade_date: '20260911',
  trend: 'G区间',
  activity: 26.68,
  level: '大牛',
  fund_net: 2.3e8,
  level3: '强',
  hits: [true, true, true],
}

/** 卡壳 = 圆角 + 四边完整边框 + token 边框色(border-t 细分隔线与 border-primary 按钮不算) */
function cardShells(root: HTMLElement): Element[] {
  return Array.from(root.querySelectorAll('*')).filter((el) => {
    const cls = el.getAttribute('class') ?? ''
    return cls.includes('border-border/') && /(^|\s)border(\s|$)/.test(cls) && /(^|\s)rounded/.test(cls)
  })
}

function setup() {
  mocks.fetchAPI.mockImplementation(async (url: string) => {
    const u = String(url)
    if (u.includes('/resonance/activity/')) return { symbol: '002636', available: false, reason: '日线不足(10 根)' }
    if (u.includes('/resonance/symbol/')) return RULE
    if (u.includes('/decision-pioneer/')) return PIONEER
    throw new Error(`unexpected url ${u}`)
  })
}

describe('DecisionCard 合并卡 · 单卡外观', () => {
  afterEach(() => cleanup())
  beforeEach(() => {
    mocks.fetchAPI.mockReset()
    mocks.setOption.mockReset()
    setup()
  })

  it('整卡只有一层卡壳、一个顶层标题(子组件不再各出卡壳/标题)', async () => {
    const { container } = render(<DecisionCard symbol="002636" market="CN" />)

    // 加载态(裸骨架): 子卡壳已让位, 只剩外层那一层
    expect(cardShells(container).length).toBe(1)

    await waitFor(() => expect(screen.getByText('GS 信号（机会/风险）')).toBeTruthy())

    // 数据态: 仍然只有一层边框 —— 不存在"卡中卡"
    const shells = cardShells(container)
    expect(shells.length).toBe(1)
    expect(shells[0].getAttribute('class')).toContain('border-border/60')

    // 一个顶层标题: 子卡的「🧭 数智决策三指标」与「三指标读数」副标题、共振面板的「三指标」标签都被 bare 抑制
    const titles = screen.getAllByText('数智决策')
    expect(titles.length).toBe(1)
    expect(titles[0].getAttribute('class')).toContain('text-[12px]')
    expect(screen.queryByText('🧭 数智决策三指标')).toBeNull()
    expect(screen.queryByText('三指标读数')).toBeNull()
    expect(screen.queryByText('三指标')).toBeNull()

    // 分段小标题(比顶层小一级), 且两段数据内容都在
    const sub = screen.getByText('共振判定')
    expect(sub.getAttribute('class')).toContain('text-[11px]')
    expect(screen.getByText('AI机构活跃度')).toBeTruthy()
    expect(screen.getByText('规则: 强')).toBeTruthy()

    // 手动刷新入口没被 bare 化吞掉(挪到卡尾)
    expect(screen.getAllByTitle('刷新').length).toBe(1)
  })

  it('默认路径不变: 不传 bare 时 DecisionPioneerCard 仍出自己的卡壳 + 标题 + 刷新', async () => {
    const { container } = render(<DecisionPioneerCard symbol="002636" market="CN" />)
    await waitFor(() => expect(screen.getByText('GS 信号（机会/风险）')).toBeTruthy())

    const shells = cardShells(container)
    expect(shells.length).toBe(1)
    expect(shells[0].getAttribute('class')).toContain('mt-3 rounded-xl border border-border/50 bg-card p-3')
    expect(screen.getByText('🧭 数智决策三指标')).toBeTruthy()
    expect(screen.getByText('GS 信号 × 机构活跃度 × L2 主力净流入（明盘口径）')).toBeTruthy()
    expect(screen.getAllByTitle('刷新').length).toBe(1)
  })
})
