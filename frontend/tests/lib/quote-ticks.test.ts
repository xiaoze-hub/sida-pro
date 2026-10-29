// @vitest-environment node
/**
 * WS tick 合并 · 价格与时刻必须同源（2026-10-29 独立复审 F3）。
 *
 * 后端 `/api/quotes/ws` 的 tick payload 只有 `{price, change_pct, prev_close, name}`
 * （见 `src/web/api/quote_stream.py::_fetch_batch_quotes`）—— **没有行情源报时**。
 *
 * 旧实现（useStocksData::onQuoteTicks）在换价时把上一轮轮询拿到的 `quote_time` /
 * `quote_date` / `daily_pnl_period` **原样保留**，于是：
 *   ① 14:00 收到的 WS 价，被配上一个 09:31 的时刻 —— 时刻不是这个价的时刻；
 *   ② `daily_pnl_period='today'` 也是上一轮（甚至上一交易日）的判断，当日盈亏按错的
 *      交易日算；
 *   ③ 反过来，"拿不到新时刻"被当成"时刻仍在"，让旧时刻继续为**新价格**背书。
 *
 * 现在的口径：WS 帧**带可靠 quote_time** 才更新时效（价格 + 时刻 + 日期 + 期间一起换）；
 * 不带就把价格与时刻**分离**：价格照更，时刻/日期/期间一律清成未知（`null` / `unknown`），
 * 由下游判成"时点未知"（`freshness=unknown`），绝不用旧时刻证明新价格。
 */
import { describe, expect, it } from 'vitest'

import { applyQuoteTicks, expireQuoteSnapshots } from '@/pages/stocks/quote-ticks'
import type { QuoteSnapshot } from '@/pages/stocks/shared'
import { freshnessFor } from '@/lib/position-risk'

const ITEMS = [{ symbol: '600111', market: 'CN' }]
const KEY = 'CN:600111'

/** 上一轮轮询（09:31）的已知快照。 */
const polled = (over: Partial<QuoteSnapshot> = {}): QuoteSnapshot => ({
  current_price: 33,
  change_pct: -1.5,
  quote_time: '2026-10-29 09:31:00',
  quote_date: '2026-10-29',
  daily_pnl_period: 'today',
  ...over,
})

const marketTrading = { localTime: '14:00', status: 'trading' }

describe('applyQuoteTicks · 不带报价时刻的 WS 帧', () => {
  it('价格更新，但时刻/日期/期间不再沿用旧值（14:00 的价不许配 09:31 的时刻）', () => {
    const next = applyQuoteTicks({ [KEY]: polled() }, ITEMS, {
      '600111': { price: 31.2, change_pct: -2.4 },
    })
    const q = next[KEY]
    expect(q.current_price).toBe(31.2)
    expect(q.change_pct).toBe(-2.4)
    // 关键：旧时刻不许留在这个新价格上
    expect(q.quote_time).toBeNull()
    expect(q.quote_date).toBeNull()
    expect(q.daily_pnl_period).toBe('unknown')
  })

  it('下游判成「时点未知」，既不冒充实时，也不拿隔日日期编历史', () => {
    const next = applyQuoteTicks({ [KEY]: polled() }, ITEMS, {
      '600111': { price: 31.2, change_pct: -2.4 },
    })
    const verdict = freshnessFor({
      price: next[KEY].current_price,
      period: next[KEY].daily_pnl_period,
      quoteTime: next[KEY].quote_time,
      market: marketTrading,
    })
    expect(verdict.freshness).toBe('unknown')
    expect(verdict.reason).toBe('no_time')
    expect(verdict.ageMinutes).toBeNull()
  })

  it('没有上一轮快照时也一样：价格有、时刻未知（不许自造时刻）', () => {
    const next = applyQuoteTicks({}, ITEMS, { '600111': { price: 31.2 } })
    expect(next[KEY].current_price).toBe(31.2)
    expect(next[KEY].quote_time).toBeNull()
    expect(next[KEY].daily_pnl_period).toBe('unknown')
  })

  /**
   * 价格逐位没变时，旧时刻描述的还是**同一个价**，配对没有被推翻 —— 这时保留不算
   * "用旧时刻为新价格作证"（价格根本没变），也避免价格不动时每 5s 把时效打回未知。
   */
  it('价格与上一轮已验证的那一对相同 → 时刻/日期/期间仍成立（不当未知）', () => {
    const next = applyQuoteTicks({ [KEY]: polled() }, ITEMS, {
      '600111': { price: 33, change_pct: -1.5 },
    })
    const q = next[KEY]
    expect(q.quote_time).toBe('2026-10-29 09:31:00')
    expect(q.quote_date).toBe('2026-10-29')
    expect(q.daily_pnl_period).toBe('today')
    const verdict = freshnessFor({
      price: q.current_price,
      period: q.daily_pnl_period,
      quoteTime: q.quote_time,
      market: marketTrading,
    })
    // 09:31 的价在 14:00 仍然按真实年龄判滞后（保留时刻 ≠ 冒充实时）
    expect(verdict.freshness).toBe('stale')
    expect(verdict.reason).toBe('intraday_age')
    expect(verdict.ageMinutes).toBe(269)
  })

  it('价格变了就必须清掉旧时刻（哪怕只差 0.01）', () => {
    const next = applyQuoteTicks({ [KEY]: polled() }, ITEMS, { '600111': { price: 33.01 } })
    expect(next[KEY].quote_time).toBeNull()
    expect(next[KEY].daily_pnl_period).toBe('unknown')
  })

  it('不改动其它标的的既有快照（只动本帧涉及的键）', () => {
    const other: QuoteSnapshot = polled({ current_price: 99 })
    const next = applyQuoteTicks(
      { 'CN:600111': polled(), 'HK:00700': other },
      ITEMS,
      { '600111': { price: 31.2 } },
    )
    expect(next['HK:00700']).toEqual(other)
  })

  it('非数值价格的脏帧被忽略（不清空已有价，也不写坏值）', () => {
    const prev = { [KEY]: polled() }
    const next = applyQuoteTicks(prev, ITEMS, {
      '600111': { price: Number.NaN } as unknown as { price: number },
    })
    expect(next[KEY]).toEqual(prev[KEY])
  })
})

describe('applyQuoteTicks · 跨日同价（B3）', () => {
  /** 昨天收盘留下的已验证配对：价格 33、时刻 15:00、日期昨天、期间「昨天判的 today」。 */
  const yesterday = polled({
    current_price: 33,
    change_pct: 0,
    quote_time: '2026-10-28 15:00:00',
    quote_date: '2026-10-28',
  })

  it('价格没变但涨跌幅变了 → 不沿用旧日期/期间（昨天 today + 今天新涨跌幅 = 串日）', () => {
    const next = applyQuoteTicks({ [KEY]: yesterday }, ITEMS, {
      '600111': { price: 33, change_pct: 1.2 },
    })
    const q = next[KEY]
    expect(q.current_price).toBe(33)
    expect(q.change_pct).toBe(1.2)
    // 价格没变 ≠ 这一对没被推翻：涨跌幅是新的一天的，旧日期/期间不能再给它作证
    expect(q.quote_time).toBeNull()
    expect(q.quote_date).toBeNull()
    expect(q.daily_pnl_period).toBe('unknown')
  })

  it('同价同涨跌幅（没有任何新信息）仍保留已验证的一对', () => {
    const next = applyQuoteTicks({ [KEY]: yesterday }, ITEMS, {
      '600111': { price: 33, change_pct: 0 },
    })
    expect(next[KEY].quote_time).toBe('2026-10-28 15:00:00')
    expect(next[KEY].quote_date).toBe('2026-10-28')
    expect(next[KEY].daily_pnl_period).toBe('today')
  })
})

describe('expireQuoteSnapshots · 本地有效期到期后的降级', () => {
  it('清掉时刻/日期/期间与涨跌幅，但保留价格（读数可陈列、时效不冒充）', () => {
    const expired = expireQuoteSnapshots({
      [KEY]: polled(),
      'HK:00700': polled({ current_price: 380, change_pct: 2.5 }),
    })
    expect(expired[KEY].current_price).toBe(33)
    expect(expired[KEY].quote_time).toBeNull()
    expect(expired[KEY].quote_date).toBeNull()
    expect(expired[KEY].daily_pnl_period).toBe('unknown')
    expect(expired[KEY].change_pct).toBeNull()
    expect(expired['HK:00700'].current_price).toBe(380)
    const verdict = freshnessFor({
      price: expired[KEY].current_price,
      period: expired[KEY].daily_pnl_period,
      quoteTime: expired[KEY].quote_time,
      market: marketTrading,
    })
    expect(verdict.freshness).toBe('unknown')
  })

  it('没有可清的内容时返回等价表（不制造新引用也无害）', () => {
    expect(expireQuoteSnapshots({})).toEqual({})
  })
})

describe('applyQuoteTicks · 带可靠报价时刻的 WS 帧', () => {
  it('时刻/日期/期间随价格一起换（价格与时刻同源配对）', () => {
    const next = applyQuoteTicks({ [KEY]: polled() }, ITEMS, {
      '600111': {
        price: 31.2,
        change_pct: -2.4,
        quote_time: '2026-10-29 13:58:00',
        quote_date: '2026-10-29',
        daily_pnl_period: 'today',
      },
    })
    const q = next[KEY]
    expect(q.quote_time).toBe('2026-10-29 13:58:00')
    expect(q.quote_date).toBe('2026-10-29')
    expect(q.daily_pnl_period).toBe('today')
    const verdict = freshnessFor({
      price: q.current_price,
      period: q.daily_pnl_period,
      quoteTime: q.quote_time,
      market: marketTrading,
    })
    expect(verdict.freshness).toBe('current')
    expect(verdict.ageMinutes).toBe(2)
  })

  it('带时刻但缺 period → period 未知（不沿用上一轮 today）', () => {
    const next = applyQuoteTicks({ [KEY]: polled() }, ITEMS, {
      '600111': { price: 31.2, quote_time: '2026-10-29 13:58:00' },
    })
    expect(next[KEY].daily_pnl_period).toBe('unknown')
    expect(next[KEY].quote_date).toBeNull()
  })

  it('空串/空白时刻等同于没有时刻（不把 "" 当时刻）', () => {
    const next = applyQuoteTicks({ [KEY]: polled() }, ITEMS, {
      '600111': { price: 31.2, quote_time: '   ' },
    })
    expect(next[KEY].quote_time).toBeNull()
    expect(next[KEY].daily_pnl_period).toBe('unknown')
  })
})
