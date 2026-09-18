/**
 * 决策账本(P1-3)钉子 —— 钉的不是"能渲染", 而是**口径不许越界**:
 *  ① 样本不足不给数字(`--` + 原因), 不许前端自己算百分比;
 *  ② 未回填/null **不是 0**: 收益显示 `--`, 颜色走中性(不许把 null 当 0 上"涨红");
 *  ③ 当时没取到价就是 `--`(不拿今天的价冒充);
 *  ④ 证据快照能被人看懂(JSON → k=v), 坏 JSON 不崩。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  contextSummary,
  hitRateText,
  isFilled,
  priceText,
  retClass,
  retText,
} from '@/lib/decision-ledger'
import type { DecisionLogItem } from '@panwatch/api'

const root = resolve(__dirname, '../..')
const read = (p: string) => readFileSync(resolve(root, p), 'utf-8')

describe('命中率口径: 样本不足不给数字', () => {
  it('样本不足 → `--` 且 muted, title 带原因', () => {
    const c = hitRateText({ n: 3, hit_rate: null, insufficient: true, note: '样本不足(3 < 30), 不给命中率' })
    expect(c.text).toBe('--')
    expect(c.muted).toBe(true)
    expect(c.title).toContain('样本不足')
  })

  it('有样本 → 百分比(两位小数内), 不是 muted', () => {
    const c = hitRateText({ n: 42, hit_rate: 0.5238, insufficient: false, note: '' })
    expect(c.text).toBe('52.4%')
    expect(c.muted).toBe(false)
    expect(c.title).toContain('n=42')
  })

  it('该档不存在 → `--`(不抛错、不显示 0%)', () => {
    expect(hitRateText(undefined).text).toBe('--')
  })

  it('命中率 0 是**真值**, 不能显示成 `--`(0% 与"没数据"必须可分)', () => {
    const c = hitRateText({ n: 40, hit_rate: 0, insufficient: false, note: '' })
    expect(c.text).toBe('0.0%')
    expect(c.muted).toBe(false)
  })
})

describe('收益/价格: null 不是 0', () => {
  it('未回填 → `--`, 颜色中性', () => {
    expect(retText(null)).toBe('--')
    expect(retClass(null)).toBe('text-muted-foreground')
  })

  it('已回填 → 带符号百分比, 涨红跌绿', () => {
    expect(retText(0.1234)).toBe('+12.34%')
    expect(retText(-0.05)).toBe('-5.00%')
    expect(retClass(0.01)).toBe('text-stock-up')
    expect(retClass(-0.01)).toBe('text-stock-down')
  })

  it('收益 0 是**真值**: 显示 +0.00% 且按"未命中"侧(中性偏平)呈现——绝不显示成 --', () => {
    expect(retText(0)).toBe('+0.00%')
    expect(retClass(0)).toBe('text-stock-up') // 与后端口径一致: >0 记命中, 0 记未命中, 但显示仍是 0
  })

  it('当时没取到价 → `--`', () => {
    expect(priceText(null)).toBe('--')
    expect(priceText(10.5)).toBe('10.50')
  })
})

describe('证据快照摘要', () => {
  it('JSON → 人可读 k=v', () => {
    expect(contextSummary('{"hits":3,"trend":true}')).toBe('hits=3 · trend=true')
  })

  it('坏 JSON 不崩, 截断原样返回', () => {
    const s = contextSummary('not-json-' + 'x'.repeat(80))
    expect(s.endsWith('…')).toBe(true)
    expect(s.length).toBeLessThanOrEqual(47)
  })

  it('嵌套/空值跳过, 不渲染成 [object Object]', () => {
    expect(contextSummary('{"nested":{"a":1},"empty":null,"n":2}')).toBe('n=2')
  })
})

describe('已回填判定', () => {
  const mk = (rets: [number | null, number | null, number | null]): DecisionLogItem =>
    ({
      signal_kind: 'resonance3',
      symbol: '002361',
      trade_date: '2026-09-18',
      price_at_signal: 10,
      context: '{}',
      source: 'resonance_scan',
      outcomes: {
        t1: { ret: rets[0], hit: rets[0] == null ? null : rets[0] > 0 },
        t3: { ret: rets[1], hit: rets[1] == null ? null : rets[1] > 0 },
        t5: { ret: rets[2], hit: rets[2] == null ? null : rets[2] > 0 },
      },
      filled_at: null,
    }) as DecisionLogItem

  it('任一档有结果即算已回填(未来 K 线还没到 = 未回填)', () => {
    expect(isFilled(mk([0.01, null, null]))).toBe(true)
    expect(isFilled(mk([null, null, null]))).toBe(false)
  })
})

describe('页面与路由接线(源码级)', () => {
  const page = read('src/pages/DecisionLedger.tsx')
  const app = read('src/App.tsx')

  it('页面走统一的口径函数, 不自己写百分比/不做 toFixed', () => {
    expect(page).toMatch(/hitRateText\(/)
    expect(page).toMatch(/retText\(/)
    expect(page).not.toMatch(/\.toFixed\(/)
    expect(page).not.toMatch(/hit_rate \* 100/)
  })

  it('正文不许出现 markdown 星号(会原样渲染)', () => {
    expect(page).not.toMatch(/>[^<]{0,80}\*\*/)
  })

  it('路由与导航都挂上了', () => {
    expect(app).toContain('path="/decision-ledger"')
    expect(app).toContain("'nav.decisionLedger'")
  })
})
