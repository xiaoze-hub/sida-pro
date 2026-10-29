// @vitest-environment node
/**
 * 持仓风险检查 · 纯函数回归（2026-10-29, P2 持仓风险切片；2026-10-29 二次评审修订）。
 *
 * 这份文件钉的是**判定口径**，不是文案：
 *  1. 破位只用**用户自己设的** `defense_price`（方案 B 裁决）：没设 → `unset`，不许拿
 *     `成本 × (1-8%)` 冒充纪律位，也不许因为「没设」就判成安全。
 *  2. 缺现价 → `unknown`，既不算破位也不算安全；缺失必须能数出来（`noPrice`），
 *     不许静默从分母里消失（否则「3 只全安全」会把第 4 只缺行情的藏掉）。
 *  3. 边界值钉死：`price === defense` 算破位；距防线正好 3% 算「逼近」，3.01% 算安全。
 *  4. PG DECIMAL 经 JSON 会变成**字符串**（`"36.52"`），解析必须走 safeNum 口径。
 *  5. 不产出任何买卖结论字段 —— 只产出可核对的观测值（状态 / 距离 / 缺口）。
 *
 * 评审补充（以下每条都对应一次真实错判，先红后修）：
 *  6. **同标的多账户各自一条防线**：两账户防线 34 / 25、现价 33，必须一行破位一行安全；
 *     交换输入顺序后结论**完全不变**（旧实现按遍历顺序覆盖 `prev.defense`，会来回翻）。
 *  7. **行情滞后不得冒充破位**：`daily_pnl_period` 不是 `today` 时，旧报价只能标
 *     `行情滞后`，不进 `breached` 计数 —— 旧实现把隔日/隔周报价当实时价判破位。
 *  8. **全盈利不许叫「浮亏最重」**：`worstByPnl` 是最弱一只（可能为正），
 *     `allProfitable` 明确告诉 UI 换措辞。
 *  9. **缺口各自独立计数**：无现价 + 未设防线要同时进 `noPrice` 和 `noDefense`。
 * 10. **币种按市场标识**：HK→HKD、US→USD、CN→CNY；不许把三种币的价格混成一列裸数字。
 * 11. **缺成本不稀释**：同标的两账户，一个缺成本时不许拿它去加权摊薄另一账户的成本。
 */
import { describe, expect, it } from 'vitest'

import {
  NEAR_LINE_PCT,
  buildPositionRiskReport,
  currencyForMarket,
  evaluateDefenseLine,
  formatPriceWithCurrency,
  quoteAgeLabel,
  resolveDefenseState,
} from '@/lib/position-risk'

const pos = (over: Record<string, unknown> = {}) => ({
  id: 1,
  account_id: 1,
  account_name: '主账户',
  symbol: '600111',
  name: '北方稀土',
  market: 'CN',
  cost_price: 36.52,
  quantity: 100,
  current_price: 35.29,
  pnl_pct: -3.37,
  defense_price: 33.96,
  quote_date: '2026-10-29',
  quote_time: '2026-10-29 15:00:00',
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

describe('resolveDefenseState · 行情新鲜度闸门', () => {
  it("period='today' 才允许出破位/逼近/安全", () => {
    expect(resolveDefenseState({ price: 33, defense: 34, period: 'today' })).toMatchObject({
      state: 'breached',
      freshness: 'current',
    })
  })

  it("period='previous_trading_day' 的旧报价 → stale，不当成当前风险信号", () => {
    const r = resolveDefenseState({ price: 33, defense: 34, period: 'previous_trading_day' })
    expect(r.state).toBe('stale')
    expect(r.freshness).toBe('stale')
    // 原始距离仍保留（可核对），但状态不是 breached
    expect(r.distancePct).toBeCloseTo(-2.94, 2)
  })

  it("period='unknown' / 'mixed' 无法证明新鲜 → stale，不猜「今天」", () => {
    expect(resolveDefenseState({ price: 33, defense: 34, period: 'unknown' }).state).toBe('stale')
    expect(resolveDefenseState({ price: 33, defense: 34, period: 'mixed' }).state).toBe('stale')
    expect(resolveDefenseState({ price: 33, defense: 34, period: null }).state).toBe('stale')
  })

  it('没有现价 → unknown（freshness=missing），与防线无关', () => {
    expect(resolveDefenseState({ price: null, defense: 34, period: 'today' })).toEqual({
      state: 'unknown',
      distancePct: null,
      freshness: 'missing',
    })
  })
})

describe('quoteAgeLabel · 可验证新鲜度', () => {
  it('用注入的 now 算报价距今多久，不读系统时间', () => {
    const now = new Date(2026, 9, 29, 18, 0, 0)
    expect(quoteAgeLabel('2026-10-29 15:00:00', now)).toBe('3.0 小时前')
    expect(quoteAgeLabel('2026-10-29 17:30:00', now)).toBe('30 分钟前')
    expect(quoteAgeLabel('2026-10-27 15:00:00', now)).toBe('2.1 天前')
  })

  it('时间串缺失/无法解析 → null（宁可不显示，也不编一个时点）', () => {
    expect(quoteAgeLabel(null, new Date(2026, 9, 29, 18, 0, 0))).toBeNull()
    expect(quoteAgeLabel('', new Date(2026, 9, 29, 18, 0, 0))).toBeNull()
    expect(quoteAgeLabel('not-a-time', new Date(2026, 9, 29, 18, 0, 0))).toBeNull()
    expect(quoteAgeLabel('2026-10-29 15:00:00', null)).toBeNull()
  })
})

describe('币种', () => {
  it('按市场取币种，未知市场不猜成人民币', () => {
    expect(currencyForMarket('CN')).toBe('CNY')
    expect(currencyForMarket('HK')).toBe('HKD')
    expect(currencyForMarket('US')).toBe('USD')
    expect(currencyForMarket('hk')).toBe('HKD')
    expect(currencyForMarket('XX')).toBe('UNKNOWN')
  })

  it('价格带币种符号，港美不被读成人民币', () => {
    expect(formatPriceWithCurrency(35.29, 'CN')).toBe('¥35.29')
    expect(formatPriceWithCurrency(12.3, 'HK')).toBe('HK$12.3')
    expect(formatPriceWithCurrency(5.6, 'US')).toBe('US$5.6')
    expect(formatPriceWithCurrency(null, 'HK')).toBe('--')
  })
})

describe('buildPositionRiskReport · 逐账户', () => {
  it('空持仓：全 0 且 rows 为空，worst 为 null', () => {
    const r = buildPositionRiskReport([])
    expect(r).toMatchObject({
      total: 0,
      symbols: 0,
      accounts: 0,
      priced: 0,
      breached: 0,
      near: 0,
      safe: 0,
      stale: 0,
      noDefense: 0,
      noPrice: 0,
      noCost: 0,
      worstByPnl: null,
      bestByPnl: null,
      allProfitable: false,
      conflictingSymbols: [],
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
    expect(r.rows[0].freshness).toBe('missing')
  })

  it('未设防线：state=unset + no_defense 缺口，不是 safe', () => {
    const r = buildPositionRiskReport([pos({ defense_price: null })])
    expect(r.rows[0].defenseState).toBe('unset')
    expect(r.rows[0].defensePrice).toBeNull()
    expect(r.rows[0].gaps).toContain('no_defense')
    expect(r.noDefense).toBe(1)
    expect(r.rows[0].pnlPct).toBeCloseTo(-3.37, 2)
  })

  it('缺口独立计数：既无现价又未设防线，两个计数都要 +1', () => {
    const r = buildPositionRiskReport([
      pos({ current_price: null, defense_price: null, pnl_pct: null }),
    ])
    expect(r.rows[0].gaps).toEqual(expect.arrayContaining(['no_price', 'no_defense']))
    expect(r.noPrice).toBe(1)
    expect(r.noDefense).toBe(1)
    // 状态只有一个（无现价优先），但缺口不许互相吃掉
    expect(r.rows[0].defenseState).toBe('unknown')
    expect(r.breached + r.near + r.safe + r.stale + r.unset + r.unknown).toBe(r.total)
  })

  it('成本缺失/为 0：不回退成 0 盈亏，pnl 给 null 并记缺口', () => {
    const r = buildPositionRiskReport([pos({ cost_price: 0, pnl_pct: null })])
    expect(r.rows[0].pnlPct).toBeNull()
    expect(r.rows[0].gaps).toContain('no_cost')
    expect(r.noCost).toBe(1)
    // 防线判定不依赖成本，仍要照常给出
    expect(r.rows[0].defenseState).toBe('safe')
  })

  it('后端给了 pnl_pct 就用后端口径，不自己重算', () => {
    const r = buildPositionRiskReport([pos({ cost_price: 100, current_price: 110, pnl_pct: 9.99 })])
    expect(r.rows[0].pnlPct).toBe(9.99)
  })

  it('排序：破位（越深越前）→ 逼近 → 行情滞后 → 缺现价 → 未设防线 → 安全', () => {
    const r = buildPositionRiskReport([
      pos({ id: 1, symbol: '000001', name: '平安银行', defense_price: 30, current_price: 40 }), // safe
      pos({ id: 2, symbol: '600000', name: '浦发银行', defense_price: null }), // unset
      pos({ id: 3, symbol: '600111', name: '北方稀土', defense_price: 33.96, current_price: 33.9 }), // breached -0.18
      pos({ id: 4, symbol: '601318', name: '中国平安', defense_price: 50, current_price: 45 }), // breached -10
      pos({ id: 5, symbol: '600519', name: '贵州茅台', current_price: null }), // unknown
      pos({ id: 6, symbol: '000858', name: '五粮液', defense_price: 100, current_price: 101.5 }), // near 1.5
      pos({
        id: 7,
        symbol: '600036',
        name: '招商银行',
        defense_price: 40,
        current_price: 38,
        daily_pnl_period: 'previous_trading_day',
      }), // stale（旧价看似破位，不许混进 breached）
    ])
    expect(r.rows.map((x) => x.symbol)).toEqual([
      '601318', // breached 最深
      '600111', // breached 浅
      '000858', // near
      '600036', // stale
      '600519', // unknown
      '600000', // unset
      '000001', // safe
    ])
    expect([r.breached, r.near, r.stale, r.noPrice, r.noDefense]).toEqual([2, 1, 1, 1, 1])
    expect(r.priced).toBe(6)
  })

  it('同标的多账户各自一条防线：结论按账户归属，交换输入顺序结果不变（旧实现会来回翻）', () => {
    const a = pos({ id: 11, account_id: 1, account_name: '主账户', defense_price: 34, current_price: 33 })
    const b = pos({ id: 12, account_id: 2, account_name: '备用账户', defense_price: 25, current_price: 33 })
    const forward = buildPositionRiskReport([a, b])
    const backward = buildPositionRiskReport([b, a])

    // 不合并：两个账户两行，各自归属
    expect(forward.total).toBe(2)
    expect(forward.symbols).toBe(1)
    expect(forward.accounts).toBe(2)

    const byAccount = (r: typeof forward) =>
      r.rows
        .map((row) => [row.accountId, row.accountName, row.defensePrice, row.defenseState] as const)
        .sort((x, y) => String(x[0]).localeCompare(String(y[0])))

    expect(byAccount(forward)).toEqual([
      ['1', '主账户', 34, 'breached'],
      ['2', '备用账户', 25, 'safe'],
    ])
    // 顺序交换后逐项一致 —— 防线不许被后一行覆盖
    expect(byAccount(backward)).toEqual(byAccount(forward))
    expect([forward.breached, forward.safe]).toEqual([1, 1])
    expect([backward.breached, backward.safe]).toEqual([1, 1])
    expect(forward.conflictingSymbols).toEqual(['CN:600111'])
    expect(backward.conflictingSymbols).toEqual(['CN:600111'])
  })

  it('同标的多账户防线相同 → 仍分行，但不记为冲突', () => {
    const r = buildPositionRiskReport([
      pos({ id: 11, account_id: 1, account_name: '主账户', defense_price: 34 }),
      pos({ id: 12, account_id: 2, account_name: '备用账户', defense_price: 34 }),
    ])
    expect(r.total).toBe(2)
    expect(r.conflictingSymbols).toEqual([])
  })

  it('旧报价不许冒充实时破位：period 非 today 时不进 breached，其余仍是观测值', () => {
    const r = buildPositionRiskReport([
      pos({ symbol: '600111', defense_price: 34, current_price: 33, daily_pnl_period: 'previous_trading_day' }),
    ])
    expect(r.breached).toBe(0)
    expect(r.stale).toBe(1)
    expect(r.rows[0].defenseState).toBe('stale')
    expect(r.rows[0].freshness).toBe('stale')
    expect(r.rows[0].quoteDate).toBe('2026-10-29')
  })

  it('全盈利时 worstByPnl 是最弱的一只（正值），allProfitable 为 true', () => {
    const r = buildPositionRiskReport([
      pos({ id: 1, symbol: '600111', pnl_pct: 2.4 }),
      pos({ id: 2, symbol: '601318', pnl_pct: 8.1 }),
      pos({ id: 3, symbol: '600519', pnl_pct: 0.3 }),
    ])
    expect(r.worstByPnl?.symbol).toBe('600519')
    expect(r.worstByPnl?.pnlPct).toBeCloseTo(0.3, 2)
    expect(r.bestByPnl?.symbol).toBe('601318')
    expect(r.allProfitable).toBe(true)
  })

  it('有浮亏时 allProfitable 为 false，worstByPnl 是浮亏最重的一只', () => {
    const r = buildPositionRiskReport([
      pos({ id: 1, symbol: '600111', pnl_pct: 3.2 }),
      pos({ id: 2, symbol: '601318', pnl_pct: -12.5 }),
      pos({ id: 3, symbol: '600519', pnl_pct: null, current_price: null }),
    ])
    expect(r.allProfitable).toBe(false)
    expect(r.worstByPnl?.symbol).toBe('601318')
    expect(r.worstByPnl?.pnlPct).toBe(-12.5)
    expect(r.bestByPnl?.symbol).toBe('600111')
  })

  it('缺成本的账户不稀释另一账户的成本（不许加权成 50）', () => {
    const r = buildPositionRiskReport([
      pos({ id: 11, account_id: 1, account_name: '主账户', cost_price: 100, quantity: 100, pnl_pct: null }),
      pos({ id: 12, account_id: 2, account_name: '备用账户', cost_price: null, quantity: 100, pnl_pct: null }),
    ])
    const main = r.rows.find((x) => x.accountName === '主账户')!
    const other = r.rows.find((x) => x.accountName === '备用账户')!
    expect(main.costPrice).toBe(100)
    expect(main.pnlPct).toBeCloseTo(-64.71, 2) // (35.29-100)/100
    expect(other.costPrice).toBeNull()
    expect(other.pnlPct).toBeNull()
    expect(other.gaps).toContain('no_cost')
  })

  it('港/美/A 各自带币种，价格不被当成同币种裸数字', () => {
    const r = buildPositionRiskReport([
      pos({ id: 1, symbol: '600111', market: 'CN', current_price: 35.29, defense_price: 33.96 }),
      pos({ id: 2, symbol: '00700', name: '腾讯控股', market: 'HK', current_price: 380, defense_price: 360 }),
      pos({ id: 3, symbol: 'AAPL', name: '苹果', market: 'US', current_price: 180, defense_price: 170 }),
      pos({ id: 4, symbol: 'XXXX', name: '未知市场', market: 'SG', current_price: 10, defense_price: 9 }),
    ])
    const bySymbol = new Map(r.rows.map((x) => [x.symbol, x]))
    expect(bySymbol.get('600111')?.currency).toBe('CNY')
    expect(bySymbol.get('00700')?.currency).toBe('HKD')
    expect(bySymbol.get('AAPL')?.currency).toBe('USD')
    expect(bySymbol.get('XXXX')?.currency).toBe('UNKNOWN')
    const hk = bySymbol.get('00700')!
    expect(formatPriceWithCurrency(hk.currentPrice, hk.market)).toBe('HK$380')
    expect(formatPriceWithCurrency(bySymbol.get('AAPL')!.defensePrice, 'US')).toBe('US$170')
  })

  it('只输出观测值：行里没有方向/买卖/建议字段', () => {
    const row = buildPositionRiskReport([pos()]).rows[0]
    expect(Object.keys(row).sort()).toEqual([
      'accountId',
      'accountName',
      'costPrice',
      'currency',
      'currentPrice',
      'defensePrice',
      'defenseState',
      'distanceToDefensePct',
      'freshness',
      'gaps',
      'key',
      'market',
      'name',
      'pnlPct',
      'pricePeriod',
      'quoteDate',
      'quoteTime',
      'sharedSymbolConflict',
      'symbol',
    ])
  })
})
