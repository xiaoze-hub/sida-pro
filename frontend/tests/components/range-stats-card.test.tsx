// @vitest-environment jsdom
//
// 2026-09-18 设计稿 v2.1 §10.2④ 区间统计卡。
//
// 这层只管渲染, 但"渲染口径"本身就是验收项, 故钉死三件事:
//  ① 涨跌幅/振幅/首末价照实显示, 涨红跌绿;
//  ② 明盘/暗盘无数据时显示 `--` + 「无数据」, **绝不显示 0**(0 是"净额为 0", 两回事);
//  ③ 事件 0 次照实显示 `0 次`(0 是事实); 点 × 触发 onClear。
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import RangeStatsCard from '@/components/RangeStatsCard'
import type { KlineRangeStats } from '@panwatch/biz-ui/components/KlineChart'

afterEach(cleanup)

function stats(over: Partial<KlineRangeStats> = {}): KlineRangeStats {
  return {
    from: '2026-09-01',
    to: '2026-09-11',
    bars: 9,
    firstClose: 10.5,
    lastClose: 11.2,
    changePct: 6.67,
    amplitudePct: 28.42,
    mingNet: 123000000,
    mingDays: 3,
    darkNet: -45000000,
    darkDays: 2,
    eventCounts: { limit_up: 2, split_cluster: 1 },
    eventTotal: 3,
    priceLines: [],
    ...over,
  }
}

describe('RangeStatsCard', () => {
  it('区间/根数/首末价/涨跌幅/振幅都上屏, 涨用红(--stock-up)', () => {
    render(<RangeStatsCard stats={stats()} />)
    expect(screen.getByText(/2026-09-01 → 2026-09-11/)).toBeTruthy()
    expect(screen.getByText(/9 根/)).toBeTruthy()
    expect(screen.getByText('10.50')).toBeTruthy()
    expect(screen.getByText('11.20')).toBeTruthy()
    const pct = screen.getByText('涨跌幅 +6.67%')
    expect(pct.className).toContain('text-stock-up')
    expect(screen.getByText(/振幅 28.42%/)).toBeTruthy()
  })

  it('跌用绿(--stock-down), 符号带负号', () => {
    render(<RangeStatsCard stats={stats({ changePct: -3.2 })} />)
    const pct = screen.getByText('涨跌幅 -3.20%')
    expect(pct.className).toContain('text-stock-down')
  })

  it('明盘/暗盘按元口径显示亿/万, 并标注有值天数', () => {
    render(<RangeStatsCard stats={stats()} />)
    expect(screen.getByText('+1.23亿')).toBeTruthy() // mingNet 123000000 元(≥1亿 → 亿)
    expect(screen.getByText('-4500.00万')).toBeTruthy() // darkNet -45000000 元(<1亿 → 万, 负号保留)
    expect(screen.getByText('3天')).toBeTruthy()
    expect(screen.getByText('2天')).toBeTruthy()
  })

  it('无数据 → `--` + 「无数据」, 不显示 0', () => {
    render(<RangeStatsCard stats={stats({ mingNet: null, mingDays: 0, darkNet: null, darkDays: 0 })} />)
    expect(screen.getAllByText('--')).toHaveLength(2)
    expect(screen.getAllByText('无数据')).toHaveLength(2)
    expect(screen.queryByText('+0.00万')).toBeNull()
  })

  it('事件 0 次照实显示(0 是事实, 不是缺数据)', () => {
    render(<RangeStatsCard stats={stats({ eventCounts: {}, eventTotal: 0 })} />)
    expect(screen.getByText('0 次')).toBeTruthy()
  })

  it('有事件时给出各 kind 明细', () => {
    render(<RangeStatsCard stats={stats()} />)
    expect(screen.getByText(/3 次/)).toBeTruthy()
    expect(screen.getByText(/涨停×2 · 拆单簇×1/)).toBeTruthy()
  })

  it('点 × 触发 onClear(收起本卡)', () => {
    const onClear = vi.fn()
    render(<RangeStatsCard stats={stats()} onClear={onClear} />)
    fireEvent.click(screen.getByLabelText('收起区间统计'))
    expect(onClear).toHaveBeenCalledTimes(1)
  })
})
