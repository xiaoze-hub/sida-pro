// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

/**
 * DecisionCard 合并卡(工作台 v2 三合一, spec §4.2 去重表第 1 项): 三指标读数 + 共振判定
 * 同归 `rail.decision`, 全工作台**只此一处**。
 *
 * 本测试守的是"组合契约"而非子组件内部: 两个既有子组件各只被渲染一次(去重),
 * 且 `symbol`/`market` 原样透传(不吞参、不各自再造取数入口);
 * 两个子组件必须以 `bare` 合成(复审: 单卡外观 —— 子组件不出自己的卡壳/标题)。
 * 真实子组件下的"只有一层边框 + 只有一个顶层标题"由 decision-card-chrome.test.tsx 守。
 */
const calls = vi.hoisted(() => ({
  pioneer: [] as Array<{ symbol: string; market: string; bare?: boolean }>,
  resonance: [] as Array<{ symbol: string; bare?: boolean }>,
}))

vi.mock('@panwatch/biz-ui/components/DecisionPioneerCard', () => ({
  default: (props: { symbol: string; market: string; bare?: boolean }) => {
    calls.pioneer.push(props)
    return <div data-testid="pioneer">{`pioneer:${props.symbol}:${props.market}`}</div>
  },
}))

vi.mock('@panwatch/biz-ui/components/ResonanceVerdictPanel', () => ({
  default: (props: { symbol: string; bare?: boolean }) => {
    calls.resonance.push(props)
    return <div data-testid="resonance">{`resonance:${props.symbol}`}</div>
  },
}))

import DecisionCard from '@panwatch/biz-ui/components/workbench/DecisionCard'

describe('DecisionCard 合并卡', () => {
  afterEach(() => {
    cleanup()
    calls.pioneer.length = 0
    calls.resonance.length = 0
  })

  it('一张卡内渲染三指标读数 + 共振判定, 各只一次, 且子组件以 bare 合成', () => {
    render(<DecisionCard symbol="002636" market="CN" />)
    expect(screen.getByText('数智决策')).toBeTruthy()
    // 复审: 冗余副标题「三指标读数」已删(子卡自带标题, 会形成三级标题)
    expect(screen.queryByText('三指标读数')).toBeNull()
    expect(screen.getByText('共振判定')).toBeTruthy()
    expect(screen.getAllByTestId('pioneer')).toHaveLength(1)
    expect(screen.getAllByTestId('resonance')).toHaveLength(1)
    expect(screen.getByTestId('pioneer').textContent).toBe('pioneer:002636:CN')
    expect(screen.getByTestId('resonance').textContent).toBe('resonance:002636')
  })

  it('symbol/market 原样透传 + bare=true, 子组件各只挂载一次(无重复取数入口)', () => {
    render(<DecisionCard symbol="600519" market="US" />)
    expect(calls.pioneer).toHaveLength(1)
    expect(calls.pioneer[0]).toEqual({ symbol: '600519', market: 'US', bare: true })
    expect(calls.resonance).toHaveLength(1)
    expect(calls.resonance[0]).toEqual({ symbol: '600519', bare: true })
  })
})
