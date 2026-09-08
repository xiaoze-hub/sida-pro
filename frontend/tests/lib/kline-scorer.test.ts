import { describe, expect, it } from 'vitest'
import { buildKlineSuggestion } from '../../src/lib/kline-scorer'

// E3(2026-09-09): K线评分纯逻辑单测。覆盖 多头/空头动作映射、持仓语义、
// 空数据中性、形态强弱分档。构造最小输入, 不依赖 biz-ui 类型。

const mk = (over: Record<string, unknown>) => ({ timeframe: '1d', ...over })

describe('buildKlineSuggestion 评分与动作映射', () => {
  it('多头排列+MACD金叉+柱体为正 → score 5, buy/买入', () => {
    const r = buildKlineSuggestion(mk({ trend: '多头排列', macd_status: '金叉', macd_hist: 0.5 }) as never)
    expect(r.score).toBe(5)
    expect(r.action).toBe('buy')
    expect(r.action_label).toBe('买入')
    expect(r.tags).toContain('多头')
    expect(r.tags).toContain('MACD金叉')
  })

  it('空头排列+MACD死叉+柱体为负 → score -5, avoid/回避', () => {
    const r = buildKlineSuggestion(mk({ trend: '空头排列', macd_status: '死叉', macd_hist: -0.5 }) as never)
    expect(r.score).toBe(-5)
    expect(r.action).toBe('avoid')
    expect(r.action_label).toBe('回避')
  })

  it('持仓语义: +1 → hold/持有, -1 → reduce/减仓, 0 → watch/观望', () => {
    expect(buildKlineSuggestion(mk({ rsi_status: '超卖' }) as never, true).action).toBe('hold')
    expect(buildKlineSuggestion(mk({ rsi_status: '超买' }) as never, true).action).toBe('reduce')
    expect(buildKlineSuggestion(mk({}) as never, true).action).toBe('watch')
  })

  it('空数据 → score 0, signal 技术面中性, 无 evidence', () => {
    const r = buildKlineSuggestion(mk({}) as never)
    expect(r.score).toBe(0)
    expect(r.signal).toBe('技术面中性')
    expect(r.action).toBe('watch')
    expect(r.evidence).toHaveLength(0)
  })

  it('K线形态强弱分档: 金针探底 +2, 三只乌鸦 -2', () => {
    const up = buildKlineSuggestion(mk({ kline_patterns: [{ name: '金针探底', signal: '看涨' }] }) as never)
    expect(up.score).toBe(2)
    const down = buildKlineSuggestion(mk({ kline_patterns: [{ name: '三只乌鸦', signal: '看跌' }] }) as never)
    expect(down.score).toBe(-2)
  })
})
