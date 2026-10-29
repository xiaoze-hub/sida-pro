// @vitest-environment node
/**
 * 持仓风险检查 · 纯函数回归（2026-10-29, P2 持仓风险切片）。
 *
 * 这份文件钉的是**判定口径**，不是文案：
 *  1. 破位只用**用户自己设的** `defense_price`（方案 B 裁决）：没设 → `unset`，不许拿
 *     `成本 × (1-8%)` 冒充纪律位，也不许因为「没设」就判成安全。
 *  2. 缺现价 → `unknown`，既不算破位也不算安全；缺失必须能数出来（`noPrice`），
 *     不许静默从分母里消失（否则「3 只全安全」会把第 4 只缺行情的藏掉）。
 *  3. 边界值钉死：`price === defense` 算破位（与 `DefenseLineBanner` 的 `<=` 一致）；
 *     距防线正好 3% 算「逼近」，3.01% 算安全。
 *  4. PG DECIMAL 经 JSON 会变成**字符串**（`"36.52"`），解析必须走 safeNum 口径，
 *     否则 `"35.29" <= 33.96` 这类字符串比较会得出反向结论。
 *  5. 不产出任何买卖结论字段 —— 只产出可核对的观测值（状态 / 距离 / 缺口）。
 */
import { describe, expect, it } from 'vitest'

import {
  NEAR_LINE_PCT,
  buildPositionRiskReport,
  evaluateDefenseLine,
} from '@/lib/position-risk'

const pos = (over: Record<string, unknown> = {}) => ({
  symbol: '600111',
  name: '北方稀土',
  market: 'CN',
  cost_price: 36.52,
  quantity: 100,
  current_price: 35.29,
  pnl_pct: -3.37,
  defense_price: 33.96,
  quote_date: '2026-10-29',
  daily_pnl_period: 'today',
  ...over,
})

describe('evaluateDefenseLine', () => {
  it('缺现价或缺防线时不给判定（unknown / unset），不返回 0 距离', () => {
    expect(evaluateDefenseLine(null, 33.96)).toEqual({ state: 'unknown', distancePct: null })
    expect(evaluateDefenseLine(35.29, null)).toEqual({ state: 'unset', distancePct: null })
    expect(evaluateDefenseLine(Number.NaN, 33.96)).toEqual({ state: 'unknown', distancePct: null })
    // 防线 0 / 负数不是「防线」，按未设处理（后端 defense_price<=0 也存 NULL）
    expect(evaluateDefenseLine(35.29, 0)).toEqual({ state: 'unset', distancePct: null })
  })

  it('price === defense 算破位（<=），不是「刚好安全」', () => {
    expect(evaluateDefenseLine(33.96, 33.96).state).toBe('breached')
  })

  it('跌破防线 → breached，距防线为负', () => {
    const r = evaluateDefenseLine(33.0, 33.96)
    expect(r.state).toBe('breached')
    expect(r.distancePct).toBeCloseTo(-2.827, 3)
  })

  it('逼近区间闭合在 3%：正好 3% 算 near，3.01% 算 safe', () => {
    expect(NEAR_LINE_PCT).toBe(3)
    expect(evaluateDefenseLine(33.96 * 1.03, 33.96).state).toBe('near')
    expect(evaluateDefenseLine(33.96 * 1.0301, 33.96).state).toBe('safe')
    expect(evaluateDefenseLine(40, 33.96)).toEqual({ state: 'safe', distancePct: expect.any(Number) })
  })

  it('字符串数字（PG DECIMAL）按数值判定，不做字典序比较', () => {
    // 字典序下 "9" <= "10" 为 false（'9' > '1'），会把破位误判成安全 —— 这才是要拦的形态。
    expect(evaluateDefenseLine('9', '10').state).toBe('breached')
    // 反过来：数值 100 > 99 但字典序 "100" < "99"，误判方向相反。
    expect(evaluateDefenseLine('100', '99').state).not.toBe('breached')
    expect(evaluateDefenseLine('99', '100').state).toBe('breached')
  })
})

describe('buildPositionRiskReport', () => {
  it('空持仓：全 0 且 rows 为空，worst 为 null', () => {
    const r = buildPositionRiskReport([])
    expect(r).toMatchObject({
      total: 0, priced: 0, breached: 0, near: 0, noDefense: 0, noPrice: 0, worstByPnl: null,
    })
    expect(r.rows).toEqual([])
  })

  it('缺现价：state=unknown，缺口可数，且不被算进破位/安全', () => {
    const r = buildPositionRiskReport([pos({ current_price: null, pnl_pct: null })])
    expect(r.rows[0].defenseState).toBe('unknown')
    expect(r.rows[0].pnlPct).toBeNull()
    expect(r.rows[0].gaps).toContain('no_price')
    expect(r.noPrice).toBe(1)
    expect(r.priced).toBe(0)
    expect(r.breached).toBe(0)
  })

  it('未设防线：state=unset + no_defense 缺口，不是 safe', () => {
    const r = buildPositionRiskReport([pos({ defense_price: null })])
    expect(r.rows[0].defenseState).toBe('unset')
    expect(r.rows[0].defensePrice).toBeNull()
    expect(r.rows[0].gaps).toContain('no_defense')
    expect(r.noDefense).toBe(1)
    expect(r.rows[0].pnlPct).toBeCloseTo(-3.37, 2)
  })

  it('成本缺失/为 0：不回退成 0 盈亏，pnl 给 null 并记缺口', () => {
    const r = buildPositionRiskReport([pos({ cost_price: 0, pnl_pct: null })])
    expect(r.rows[0].pnlPct).toBeNull()
    expect(r.rows[0].gaps).toContain('no_cost')
    // 防线判定不依赖成本，仍要照常给出
    expect(r.rows[0].defenseState).toBe('safe')
  })

  it('后端给了 pnl_pct 就用后端口径，不自己重算', () => {
    const r = buildPositionRiskReport([pos({ cost_price: 100, current_price: 110, pnl_pct: 9.99 })])
    expect(r.rows[0].pnlPct).toBe(9.99)
  })

  it('排序：破位（越深越前）→ 逼近 → 缺现价 → 未设防线 → 安全', () => {
    const r = buildPositionRiskReport([
      pos({ symbol: '000001', name: '平安银行', defense_price: 30, current_price: 40 }), // safe
      pos({ symbol: '600000', name: '浦发银行', defense_price: null }), // unset
      pos({ symbol: '600111', name: '北方稀土', defense_price: 33.96, current_price: 33.9 }), // breached -0.18
      pos({ symbol: '601318', name: '中国平安', defense_price: 50, current_price: 45 }), // breached -10
      pos({ symbol: '600519', name: '贵州茅台', current_price: null }), // unknown
      pos({ symbol: '000858', name: '五粮液', defense_price: 100, current_price: 101.5 }), // near 1.5
    ])
    expect(r.rows.map((x) => x.symbol)).toEqual([
      '601318', // breached 最深
      '600111', // breached 浅
      '000858', // near
      '600519', // unknown
      '600000', // unset
      '000001', // safe
    ])
    expect([r.breached, r.near, r.noPrice, r.noDefense]).toEqual([2, 1, 1, 1])
    expect(r.priced).toBe(5)
  })

  it('worstByPnl 取浮亏最重的一只（同幅取先出现的），缺现价的不参与', () => {
    const r = buildPositionRiskReport([
      pos({ symbol: '600111', pnl_pct: -3.37 }),
      pos({ symbol: '601318', pnl_pct: -12.5 }),
      pos({ symbol: '600519', pnl_pct: null, current_price: null }),
    ])
    expect(r.worstByPnl?.symbol).toBe('601318')
    expect(r.worstByPnl?.pnlPct).toBe(-12.5)
  })

  it('同标的跨账户合并计一行，缺口按合并后判定（不重复计持仓数）', () => {
    const r = buildPositionRiskReport([
      pos({ symbol: '600111', current_price: 35.29 }),
      pos({ symbol: '600111', current_price: null, pnl_pct: null }),
    ])
    expect(r.total).toBe(1)
    expect(r.rows[0].defenseState).toBe('safe')
  })

  it('只输出观测值：行里没有方向/买卖建议字段', () => {
    const row = buildPositionRiskReport([pos()]).rows[0]
    expect(Object.keys(row).sort()).toEqual([
      'costPrice', 'currentPrice', 'defensePrice', 'defenseState',
      'distanceToDefensePct', 'gaps', 'key', 'market', 'name',
      'pnlPct', 'pricePeriod', 'quoteDate', 'symbol',
    ])
  })
})
