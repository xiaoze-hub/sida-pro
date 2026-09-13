import { describe, it, expect } from 'vitest'
import { normalizeType, parseTab, WORKBENCH_TABS, showStockOnly, DATA_OWNERSHIP } from '@/lib/workbench-tabs'

describe('workbench-tabs', () => {
  it('normalizeType 只认 index/board, 其余归 stock', () => {
    expect(normalizeType('index')).toBe('index')
    expect(normalizeType('board')).toBe('board')
    expect(normalizeType(null)).toBe('stock')
    expect(normalizeType('garbage')).toBe('stock')
  })
  it('parseTab 白名单外与空值都回落 l2', () => {
    expect(parseTab('suggest')).toBe('suggest')
    expect(parseTab('nope')).toBe('l2')
    expect(parseTab(undefined)).toBe('l2')
  })
  it('标签固定 6 个顺序固定', () => {
    expect(WORKBENCH_TABS.map((t) => t.id)).toEqual(['l2', 'suggest', 'fundamental', 'news', 'research', 'forecast'])
  })
  it('仅 stock 显示个股专属块', () => {
    expect(showStockOnly('stock')).toBe(true)
    expect(showStockOnly('index')).toBe(false)
    expect(showStockOnly('board')).toBe(false)
  })
  it('去重: 数智决策三指标与共振同归一处', () => {
    expect(DATA_OWNERSHIP.decision_indicators).toBe(DATA_OWNERSHIP.resonance_verdict)
  })
  it('去重: 关键数据点归属固定', () => {
    expect(DATA_OWNERSHIP.kline_main).toBe('band2.chart')
    expect(DATA_OWNERSHIP.orderbook_ladder).toBe('tab.l2')
    expect(DATA_OWNERSHIP.technical_suggestion).toBe('band1.suggestStrip')
    expect(DATA_OWNERSHIP.intraday_monitor).toBe('tab.suggest')
  })
})
