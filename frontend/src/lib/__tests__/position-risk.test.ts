// @vitest-environment node
/**
 * 持仓风险检查 · 纯函数回归（2026-10-29, P2 持仓风险切片；2026-10-29 二/三次评审修订）。
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
 *
 * 三次评审（日内陈旧报价）：
 * 12. **`today` ≠ 实时**：09:31 的报价在 14:03 仍被后端判 `today`，但它不是「现在的价」。
 *     必须按**服务器市场墙钟**与报价时刻之差判时刻层，超窗即 `stale`，不进 `breached`。
 * 13. **证明不了就 `unknown`**：无 `quote_time`（WS tick 只剩裸价）、无服务器市场时钟、
 *     报价晚于市场时钟 —— 一律不下破位结论，不许退回「period==='today' 就算实时」。
 * 14. **服务器说非交易时段 ⇒ 定格价**：当日最后一笔报价照判，但 freshness 是
 *     `session_frozen`（UI 必须写「非交易时段」），不许写成实时。
 * 15. **计数与表格一致**：`stale = staleKnown + staleUnverified`，
 *     `breached+near+safe+stale+unset+unknown === total`，`noDefense` 与「防线=未设」同数。
 */
import { describe, expect, it } from 'vitest'

import {
  NEAR_LINE_PCT,
  QUOTE_FRESH_WINDOW_MINUTES,
  buildPositionRiskReport,
  currencyForMarket,
  evaluateDefenseLine,
  formatPriceWithCurrency,
  freshnessFor,
  quoteAgeLabel,
  quoteStampNote,
  isVendorStamp,
  resolveDefenseState,
  wallClockMinutes,
} from '@/lib/position-risk'
import type { MarketSessionMap, PositionRiskInput } from '@/lib/position-risk'

/** 服务器事实夹具：CN 交易日 15:03（`/stocks/markets/status` 的 local_time + status）。 */
const TRADING: MarketSessionMap = {
  CN: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
  HK: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
  US: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
  SG: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
}

const report = (rows: PositionRiskInput[], sessions: MarketSessionMap = TRADING) =>
  buildPositionRiskReport(rows, { marketSessions: sessions })

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

describe('wallClockMinutes · 只读字符串里的市场当地时分', () => {
  it('兼容 "YYYY-MM-DD HH:MM:SS" 与 ISO 带时区', () => {
    expect(wallClockMinutes('2026-10-29 15:00:00')).toBe(15 * 60)
    expect(wallClockMinutes('2026-10-29T09:31:12+08:00')).toBe(9 * 60 + 31)
    expect(wallClockMinutes('09:31')).toBe(9 * 60 + 31)
  })

  it('解析不出 / 非法值 → null（不猜成 0 点）', () => {
    expect(wallClockMinutes(null)).toBeNull()
    expect(wallClockMinutes('')).toBeNull()
    expect(wallClockMinutes('not-a-time')).toBeNull()
    expect(wallClockMinutes('2026-10-29 25:00:00')).toBeNull()
  })
})

describe('resolveDefenseState · 行情时效闸门（日期层 + 时刻层）', () => {
  it('交易日 + 交易中 + 报价在窗口内 → current，照常判破位', () => {
    expect(
      resolveDefenseState({
        price: 33,
        defense: 34,
        period: 'today',
        quoteTime: '2026-10-29 15:00:00',
        market: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
      }),
    ).toMatchObject({ state: 'breached', freshness: 'current', reason: 'ok', ageMinutes: 3 })
  })

  it("period='previous_trading_day' 的旧报价 → stale，不当成当前风险信号", () => {
    const r = resolveDefenseState({
      price: 33,
      defense: 34,
      period: 'previous_trading_day',
      quoteTime: '2026-10-28 15:00:00',
      market: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
    })
    expect(r.state).toBe('stale')
    expect(r.freshness).toBe('stale')
    expect(r.reason).toBe('period')
    // 原始距离仍保留（可核对），但状态不是 breached
    expect(r.distancePct).toBeCloseTo(-2.94, 2)
  })

  it("period='unknown' / 'mixed' 无法证明新鲜 → stale，不猜「今天」", () => {
    const base = { price: 33, defense: 34, market: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' } }
    expect(resolveDefenseState({ ...base, period: 'unknown' }).state).toBe('stale')
    expect(resolveDefenseState({ ...base, period: 'mixed' }).state).toBe('stale')
    expect(resolveDefenseState({ ...base, period: null }).state).toBe('stale')
  })

  it('没有现价 → unknown（freshness=missing），与防线无关', () => {
    expect(
      resolveDefenseState({
        price: null,
        defense: 34,
        period: 'today',
        quoteTime: '2026-10-29 15:00:00',
        market: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
      }),
    ).toMatchObject({ state: 'unknown', distancePct: null, freshness: 'missing' })
  })

  it('【三次评审】09:31 的报价在 14:03 不许判破位（同一交易日也算陈旧）', () => {
    const r = resolveDefenseState({
      price: 33,
      defense: 34,
      period: 'today',
      quoteTime: '2026-10-29 09:31:00',
      market: { localTime: '14:03', localDate: '2026-10-29', status: 'trading' },
    })
    expect(r.state).toBe('stale')
    expect(r.freshness).toBe('stale')
    expect(r.reason).toBe('intraday_age')
    expect(r.ageMinutes).toBe(272)
    // 距离仍陈列（可核对），但不许进 breached
    expect(r.distancePct).toBeCloseTo(-2.94, 2)
  })

  it('【三次评审】没有报价时刻（WS tick 只剩裸价）→ unknown，不冒充实时', () => {
    const r = resolveDefenseState({
      price: 33,
      defense: 34,
      period: 'today',
      quoteTime: null,
      market: { localTime: '14:03', localDate: '2026-10-29', status: 'trading' },
    })
    expect(r.state).toBe('stale')
    expect(r.freshness).toBe('unknown')
    expect(r.reason).toBe('no_time')
  })

  it('【三次评审】没有服务器市场时钟 → unknown（不读本机时钟当参考）', () => {
    const r = resolveDefenseState({
      price: 33,
      defense: 34,
      period: 'today',
      quoteTime: '2026-10-29 15:00:00',
      market: null,
    })
    expect(r.state).toBe('stale')
    expect(r.freshness).toBe('unknown')
    expect(r.reason).toBe('no_clock')
  })

  it('【三次评审】报价晚于服务器市场时钟（跨日残留/时钟异常）→ unknown', () => {
    const r = freshnessFor({
      price: 33,
      period: 'today',
      quoteTime: '2026-10-29 20:00:00',
      market: { localTime: '10:00', localDate: '2026-10-29', status: 'trading' },
    })
    expect(r.freshness).toBe('unknown')
    expect(r.reason).toBe('clock_skew')
  })

  it('【三次评审】服务器说已收盘 → 当日报价是定格价：可判，但标 session_frozen', () => {
    const r = resolveDefenseState({
      price: 33,
      defense: 34,
      period: 'today',
      quoteTime: '2026-10-29 15:00:00',
      market: { localTime: '19:40', localDate: '2026-10-29', status: 'after_hours' },
    })
    expect(r.state).toBe('breached')
    expect(r.freshness).toBe('session_frozen')
    expect(r.ageMinutes).toBe(280)
  })

  it('【三次评审】市场状态未知 → 不下结论（不把 unknown 当交易中）', () => {
    const r = freshnessFor({
      price: 33,
      period: 'today',
      quoteTime: '2026-10-29 15:00:00',
      market: { localTime: '15:03', localDate: '2026-10-29', status: 'unknown' },
    })
    expect(r.freshness).toBe('unknown')
  })

  it('阈值是推断值：默认 15 分钟，且只在「交易中」这一层收紧', () => {
    expect(QUOTE_FRESH_WINDOW_MINUTES).toBe(15)
    const at = (quote: string) =>
      freshnessFor({
        price: 33,
        period: 'today',
        quoteTime: `2026-10-29 ${quote}:00`,
        market: { localTime: '15:03', localDate: '2026-10-29', status: 'trading' },
      }).freshness
    expect(at('14:48')).toBe('current') // 恰好 15 分钟
    expect(at('14:47')).toBe('stale') // 16 分钟
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
    const r = report([])
    expect(r).toMatchObject({
      total: 0,
      symbols: 0,
      accounts: 0,
      priced: 0,
      breached: 0,
      near: 0,
      safe: 0,
      stale: 0,
      staleKnown: 0,
      staleUnverified: 0,
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
    const r = report([pos({ current_price: null, pnl_pct: null })])
    expect(r.rows[0].defenseState).toBe('unknown')
    expect(r.rows[0].pnlPct).toBeNull()
    expect(r.rows[0].gaps).toContain('no_price')
    expect(r.noPrice).toBe(1)
    expect(r.priced).toBe(0)
    expect(r.breached).toBe(0)
    expect(r.rows[0].freshness).toBe('missing')
  })

  it('未设防线：state=unset + no_defense 缺口，不是 safe', () => {
    const r = report([pos({ defense_price: null })])
    expect(r.rows[0].defenseState).toBe('unset')
    expect(r.rows[0].defensePrice).toBeNull()
    expect(r.rows[0].gaps).toContain('no_defense')
    expect(r.noDefense).toBe(1)
    expect(r.rows[0].pnlPct).toBeCloseTo(-3.37, 2)
  })

  it('缺口独立计数：既无现价又未设防线，两个计数都要 +1', () => {
    const r = report([pos({ current_price: null, defense_price: null, pnl_pct: null })])
    expect(r.rows[0].gaps).toEqual(expect.arrayContaining(['no_price', 'no_defense']))
    expect(r.noPrice).toBe(1)
    expect(r.noDefense).toBe(1)
    // 状态只有一个（无现价优先），但缺口不许互相吃掉
    expect(r.rows[0].defenseState).toBe('unknown')
    expect(r.breached + r.near + r.safe + r.stale + r.unset + r.unknown).toBe(r.total)
  })

  it('成本缺失/为 0：不回退成 0 盈亏，pnl 给 null 并记缺口', () => {
    const r = report([pos({ cost_price: 0, pnl_pct: null })])
    expect(r.rows[0].pnlPct).toBeNull()
    expect(r.rows[0].gaps).toContain('no_cost')
    expect(r.noCost).toBe(1)
    // 防线判定不依赖成本，仍要照常给出
    expect(r.rows[0].defenseState).toBe('safe')
  })

  it('后端给了 pnl_pct 就用后端口径，不自己重算', () => {
    const r = report([pos({ cost_price: 100, current_price: 110, pnl_pct: 9.99 })])
    expect(r.rows[0].pnlPct).toBe(9.99)
  })

  it('排序：破位（越深越前）→ 逼近 → 时效未证实 → 缺现价 → 未设防线 → 安全', () => {
    const r = report([
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
        quote_time: '2026-10-28 15:00:00',
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
    const forward = report([a, b])
    const backward = report([b, a])

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
    const r = report([
      pos({ id: 11, account_id: 1, account_name: '主账户', defense_price: 34 }),
      pos({ id: 12, account_id: 2, account_name: '备用账户', defense_price: 34 }),
    ])
    expect(r.total).toBe(2)
    expect(r.conflictingSymbols).toEqual([])
  })

  it('旧报价不许冒充实时破位：period 非 today 时不进 breached，其余仍是观测值', () => {
    const r = report([
      pos({
        symbol: '600111',
        defense_price: 34,
        current_price: 33,
        daily_pnl_period: 'previous_trading_day',
        quote_time: '2026-10-28 15:00:00',
      }),
    ])
    expect(r.breached).toBe(0)
    expect(r.stale).toBe(1)
    expect(r.rows[0].defenseState).toBe('stale')
    expect(r.rows[0].freshness).toBe('stale')
    expect(r.rows[0].quoteDate).toBe('2026-10-29')
  })

  it('【三次评审】同日 09:31 报价在 14:03 不进 breached，且能说清「多久之前」', () => {
    const r = report([
      pos({ symbol: '600111', defense_price: 34, current_price: 33, quote_time: '2026-10-29 09:31:00' }),
    ], { CN: { localTime: '14:03', localDate: '2026-10-29', status: 'trading' } })
    expect(r.breached).toBe(0)
    expect(r.stale).toBe(1)
    expect(r.staleKnown).toBe(1)
    expect(r.staleUnverified).toBe(0)
    const row = r.rows[0]
    expect(row.defenseState).toBe('stale')
    expect(row.freshness).toBe('stale')
    expect(row.staleReason).toBe('intraday_age')
    expect(row.marketStatus).toBe('trading')
    expect(row.quoteLocalTime).toBe('09:31')
    expect(row.marketLocalTime).toBe('14:03')
    expect(row.quoteAgeMinutes).toBe(272)
    expect(r.breached + r.near + r.safe + r.stale + r.unset + r.unknown).toBe(r.total)
  })

  it('【三次评审】无报价时刻 / 无市场时钟 → staleUnverified 单独计数，不混进「已知陈旧」', () => {
    const r = report(
      [
        pos({ id: 1, symbol: '600111', quote_time: null }),
        pos({ id: 2, symbol: '601318', quote_time: '2026-10-29 09:31:00' }),
      ],
      { CN: { localTime: '14:03', localDate: '2026-10-29', status: 'trading' } },
    )
    expect(r.stale).toBe(2)
    expect(r.staleUnverified).toBe(1)
    expect(r.staleKnown).toBe(1)
    expect(r.breached).toBe(0)
  })

  it('【三次评审】缺服务器市场时钟时全部不下结论（宁可未知，不猜实时）', () => {
    const r = report([pos()], null)
    expect(r.rows[0].defenseState).toBe('stale')
    expect(r.rows[0].freshness).toBe('unknown')
    expect(r.rows[0].staleReason).toBe('no_clock')
    expect(r.breached).toBe(0)
  })

  it('全盈利时 worstByPnl 是最弱的一只（正值），allProfitable 为 true', () => {
    const r = report([
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
    const r = report([
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
    const r = report([
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
    const r = report([
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
    const row = report([pos()]).rows[0]
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
      'marketLocalTime',
      'marketStatus',
      'name',
      'pnlPct',
      'pricePeriod',
      'quoteAgeMinutes',
      'quoteDate',
      'quoteDateBasis',
      'quoteLocalTime',
      'quoteTime',
      'quoteTimeKind',
      'sharedSymbolConflict',
      'staleReason',
      'symbol',
    ])
  })
})

/**
 * 2026-10-29 F4：TQ 只给得到「日」。
 *
 * TQ 快照没有任何报时字段（26 个键实测无 time/date），只能靠**同标的日线收盘价与现价
 * 配对**证明"这份价属于今天"。于是时效判定多出一条路径：
 *  - 日期层成立（period='today'）+ 服务器说非交易时段 ⇒ 当日最近报价（收盘口径），可判，
 *    但必须落 `date_only`，UI 写"日线口径/无成交时刻"；
 *  - 交易中 ⇒ 仍是 unknown（缺时刻 = 不知道是不是几分钟前的价）；
 *  - 日期对不上服务器市场日期 ⇒ stale/period（不是 unknown）。
 */
describe('resolveDefenseState · 源只到日（TQ 日线口径，date_only）', () => {
  const base = { price: 33, defense: 34, quoteTime: null, quoteDate: '2026-10-29' }

  it('非交易时段 + 日期已证当日 → 可判（session_frozen），但原因必须是 date_only', () => {
    const r = resolveDefenseState({
      ...base,
      period: 'today',
      market: { localTime: '19:40', localDate: '2026-10-29', status: 'closed' },
    })
    expect(r.state).toBe('breached')
    expect(r.freshness).toBe('session_frozen')
    expect(r.reason).toBe('date_only')
    // 没有时刻 ⇒ 不给"距今多久"（不许编）
    expect(r.ageMinutes).toBeNull()
  })

  it('交易中 + 只有日期 → unknown，绝不当实时（防线落 stale）', () => {
    const r = resolveDefenseState({
      ...base,
      period: 'today',
      market: { localTime: '14:03', localDate: '2026-10-29', status: 'trading' },
    })
    expect(r.state).toBe('stale')
    expect(r.freshness).toBe('unknown')
    expect(r.reason).toBe('date_only')
  })

  it('报价日期与服务器市场日期不一致 → stale/period（已知不是当下）', () => {
    const r = resolveDefenseState({
      ...base,
      quoteDate: '2026-10-28',
      period: 'today',
      market: { localTime: '19:40', localDate: '2026-10-29', status: 'closed' },
    })
    expect(r.freshness).toBe('stale')
    expect(r.reason).toBe('period')
  })

  it('没有服务器市场时钟 → unknown/date_only（不知道是不是非交易时段）', () => {
    const r = freshnessFor({ ...base, period: 'today', market: null })
    expect(r.freshness).toBe('unknown')
    expect(r.reason).toBe('date_only')
  })

  it('日期层也没证成（period 非 today）→ 仍是 no_time，不许退化成"日期版实时"', () => {
    const r = freshnessFor({
      ...base,
      quoteDate: null,
      period: 'unknown',
      market: { localTime: '19:40', localDate: '2026-10-29', status: 'closed' },
    })
    expect(r.freshness).toBe('unknown')
    expect(r.reason).toBe('no_time')
  })

  it('腾讯 parts[30] 是"源快照刷新时间"：语义原样带到行上，不改时效判定', () => {
    const row = report(
      [pos({ quote_time: '2026-10-29 16:14:57', quote_time_kind: 'snapshot_refresh' })],
      { CN: { localTime: '19:40', localDate: '2026-10-29', status: 'after_hours' } },
    ).rows[0]
    expect(row.quoteTimeKind).toBe('snapshot_refresh')
    expect(row.freshness).toBe('session_frozen')
    expect(row.staleReason).toBe('ok')
  })

  it('TQ 行：日期证据来源带到行上（daily_bar_close），quoteTime 为空', () => {
    const row = report([
      pos({ quote_time: null, quote_date_basis: 'daily_bar_close', daily_pnl_period: 'today' }),
    ]).rows[0]
    expect(row.quoteDateBasis).toBe('daily_bar_close')
    expect(row.quoteTime).toBeNull()
    expect(row.quoteDate).toBe('2026-10-29')
  })
})

/**
 * 源报时语义按市场分（2026-10-29 复审）：
 *  - CN 实测是**快照刷新时点**（收盘后仍刷到 16:14、停牌股同样刷新）⇒ 可以断言"非成交"；
 *  - 港/美实测落在收盘竞价窗口（HK 16:08:2x / US 16:00:01），可能就是最后一笔成交
 *    ⇒ **不许**沿用 CN 那句断言，只能写"是否成交时刻未核实"。
 */
describe('quoteStampNote · 源报时语义（CN 已证实 / 港美未核实）', () => {
  it('CN snapshot_refresh → 源快照时间 + 非成交时刻', () => {
    expect(quoteStampNote('snapshot_refresh', '16:14')).toBe('源快照时间 16:14（非成交时刻）')
    expect(quoteStampNote('snapshot_refresh')).toBe('源快照时间（非成交时刻）')
  })

  it('港美 vendor_stamp → 只写"源报时/未核实"，绝不断言非成交', () => {
    const note = quoteStampNote('vendor_stamp', '16:08')
    expect(note).toContain('源报时 16:08')
    expect(note).toContain('未核实')
    expect(note).not.toContain('非成交')
    expect(isVendorStamp('vendor_stamp')).toBe(true)
    expect(isVendorStamp('snapshot_refresh')).toBe(true)
    expect(isVendorStamp('')).toBe(false)
    expect(isVendorStamp(null)).toBe(false)
  })

  it('没有语义就不加任何断言（不许自造"成交时间"）', () => {
    expect(quoteStampNote('', '16:00')).toBe('')
    expect(quoteStampNote(null, null)).toBe('')
  })

  it('vendor_stamp 不改变时效判定：交易中过期照样 stale，不许当实时', () => {
    const row = report(
      [pos({ quote_time: '2026-10-29 09:31:00', quote_time_kind: 'vendor_stamp' })],
      { CN: { localTime: '14:03', localDate: '2026-10-29', status: 'trading' } },
    ).rows[0]
    expect(row.quoteTimeKind).toBe('vendor_stamp')
    expect(row.freshness).toBe('stale')
    expect(row.staleReason).toBe('intraday_age')
  })
})
