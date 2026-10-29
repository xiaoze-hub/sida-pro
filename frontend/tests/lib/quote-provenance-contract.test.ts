// @vitest-environment node
/**
 * 行情溯源 · 前后端契约（2026-10-29 F3 / B3）。
 *
 * 后端这一轮下沉了两件事（见 `src/web/api/quote_stream.py::_proven_quote_stamp`
 * 与 `src/web/api/stocks.py::get_market_status`）：
 *   ① WS `quote.tick` 的每个标的带 `quote_time` / `quote_date` / `daily_pnl_period`，
 *      值来自**行情源报文**（腾讯 parts[30]）；源不报时则为 `null` + `unknown`。
 *   ② `/stocks/markets/status` 每个市场多一个 `local_date`（该市场时区的当天日期），
 *      `local_time` 仍是 `HH:mm`。
 *
 * 这份测试把"前端真消费"钉住：
 *   - 帧里有源报时 ⇒ 价格、时刻、日期、期间**一起换**，防线判当前；
 *   - 帧里没有源报时 ⇒ 只更价，时效未知（绝不由前端补一个时刻）；
 *   - 市场状态缺 `local_date` ⇒ **HH:mm 不得自证**"同一分钟"（跨日会算出 age=0，正是
 *     复审 B3 的错判形态），一律 `unknown/period_unknown`；
 *   - 报价日期与市场日期不同天（同 HH:mm、同价）⇒ `stale/period`，不进 breached；
 *   - `verified=false`（续期失败）⇒ `stale/unknown/refresh_failed`；
 *   - 市场不同时区（CN/HK/US）各用自己的 `local_date`，币种不串。
 */
import { describe, expect, it } from 'vitest'

import { freshnessFor, resolveDefenseState, buildPositionRiskReport, currencyForMarket } from '@/lib/position-risk'
import type { MarketSessionInfo } from '@/lib/position-risk'
import { applyQuoteTicks } from '@/pages/stocks/quote-ticks'
import type { QuoteSnapshot } from '@/pages/stocks/shared'
import { buildMarketSessions } from '@/pages/stocks/PositionRiskSection'

const DAY = '2026-10-29'
const PREV = '2026-10-28'

const ITEMS = [{ symbol: '600111', market: 'CN' }]
const KEY = 'CN:600111'

const polled = (over: Partial<QuoteSnapshot> = {}): QuoteSnapshot => ({
  current_price: 33,
  change_pct: -1.5,
  quote_time: `${DAY} 09:31:00`,
  quote_date: DAY,
  daily_pnl_period: 'today',
  ...over,
})

/** 后端 `/stocks/markets/status` 的真实行形状（2026-10-29 起带 local_date）。 */
const backendStatusRow = (over: Record<string, unknown> = {}) => ({
  code: 'CN',
  name: 'A股',
  status: 'trading',
  status_text: '交易中',
  is_trading: true,
  sessions: ['09:30-11:30', '13:00-15:00'],
  local_time: '09:31',
  local_date: DAY,
  timezone: 'Asia/Shanghai',
  ...over,
})

describe('WS 帧带源报时 → 价格与时刻同源，时效可判', () => {
  it('5s 一帧的 tick 带 quote_time/date/period ⇒ 防线判「当前」（不再每帧打回未知）', () => {
    const next = applyQuoteTicks(
      { [KEY]: polled({ quote_time: `${DAY} 13:50:00` }) },
      ITEMS,
      {
        '600111': {
          price: 30,
          change_pct: -3.2,
          quote_time: `${DAY}T13:58:00+08:00`,
          quote_date: DAY,
          daily_pnl_period: 'today',
        },
      },
    )
    const q = next[KEY]
    expect(q.current_price).toBe(30)
    expect(q.quote_time).toBe(`${DAY}T13:58:00+08:00`)
    expect(q.quote_date).toBe(DAY)

    const verdict = resolveDefenseState({
      price: q.current_price,
      defense: 34,
      period: q.daily_pnl_period,
      quoteTime: q.quote_time,
      quoteDate: q.quote_date,
      market: { localTime: '14:00', localDate: DAY, status: 'trading' },
    })
    expect(verdict.freshness).toBe('current')
    expect(verdict.state).toBe('breached')
    expect(verdict.ageMinutes).toBe(2)
  })

  it('源没报时（TQ/降级源）⇒ 只更价，时刻/日期/期间仍是未知（不许前端补）', () => {
    const next = applyQuoteTicks({ [KEY]: polled() }, ITEMS, {
      '600111': { price: 31.2, change_pct: -2.4, quote_time: null, quote_date: null, daily_pnl_period: 'unknown' },
    })
    expect(next[KEY].quote_time).toBeNull()
    expect(next[KEY].quote_date).toBeNull()
    expect(next[KEY].daily_pnl_period).toBe('unknown')
  })
})

describe('市场状态缺 local_date ⇒ HH:mm 不得自证同一分钟', () => {
  it('报价日期已知、市场只有 HH:MM ⇒ unknown/period_unknown（不判当前）', () => {
    const v = freshnessFor({
      price: 33,
      period: 'today',
      quoteDate: DAY,
      quoteTime: `${DAY} 09:31:00`,
      market: { localTime: '09:31', status: 'trading' },
    })
    expect(v.freshness).toBe('unknown')
    expect(v.reason).toBe('period_unknown')
  })

  it('报价缺日期（只有时刻）、市场有日期 ⇒ 同样 unknown/period_unknown', () => {
    const v = freshnessFor({
      price: 33,
      period: 'today',
      quoteTime: '09:31:00',
      market: { localTime: '09:31', localDate: DAY, status: 'trading' },
    })
    expect(v.freshness).toBe('unknown')
    expect(v.reason).toBe('period_unknown')
  })

  it('两边都有日期且相同 ⇒ 控制组仍然判当前（不是把所有情况都用保守值兜住）', () => {
    const v = freshnessFor({
      price: 33,
      period: 'today',
      quoteDate: DAY,
      quoteTime: `${DAY} 09:31:00`,
      market: { localTime: '09:31', localDate: DAY, status: 'trading' },
    })
    expect(v.freshness).toBe('current')
    expect(v.ageMinutes).toBe(0)
  })
})

describe('跨日同价（昨天的日期 + 今天的同分钟）', () => {
  const yesterday: MarketSessionInfo = { localTime: '09:31', localDate: DAY, status: 'trading' }

  it('昨天 09:31 的价配今天 09:31 的钟 ⇒ stale/period，绝不进 breached', () => {
    const verdict = resolveDefenseState({
      price: 33,
      defense: 34,
      period: 'today',
      quoteDate: PREV,
      quoteTime: `${PREV} 09:31:00`,
      market: yesterday,
    })
    expect(verdict.freshness).toBe('stale')
    expect(verdict.reason).toBe('period')
    expect(verdict.state).toBe('stale')
    expect(verdict.ageMinutes).toBeNull()
  })

  it('同价同涨跌幅的 WS 帧不会把昨天的日期"续"到今天', () => {
    const next = applyQuoteTicks(
      { [KEY]: polled({ quote_time: `${PREV} 15:00:00`, quote_date: PREV, change_pct: 0 }) },
      ITEMS,
      { '600111': { price: 33, change_pct: 0 } },
    )
    expect(next[KEY].quote_date).toBe(PREV)
    const verdict = resolveDefenseState({
      price: next[KEY].current_price,
      defense: 34,
      period: next[KEY].daily_pnl_period,
      quoteTime: next[KEY].quote_time,
      quoteDate: next[KEY].quote_date,
      market: yesterday,
    })
    expect(verdict.freshness).toBe('stale')
    expect(verdict.state).toBe('stale')
  })
})

describe('多市场 / 多时区', () => {
  it('HK 用香港当地日期：同一天 → 当前；差一天 → stale（不拿 CN 的日期顶）', () => {
    const fresh = freshnessFor({
      price: 380,
      period: 'today',
      quoteDate: DAY,
      quoteTime: `${DAY} 10:00:00`,
      market: { localTime: '10:05', localDate: DAY, status: 'trading' },
    })
    expect(fresh.freshness).toBe('current')

    const stale = freshnessFor({
      price: 380,
      period: 'today',
      quoteDate: PREV,
      quoteTime: `${PREV} 10:00:00`,
      market: { localTime: '10:05', localDate: DAY, status: 'trading' },
    })
    expect(stale.freshness).toBe('stale')
    expect(stale.reason).toBe('period')
  })

  it('仓位行按市场出币种，不默认人民币', () => {
    expect(currencyForMarket('CN')).toBe('CNY')
    expect(currencyForMarket('HK')).toBe('HKD')
    expect(currencyForMarket('US')).toBe('USD')
  })
})

describe('前端真消费后端 local_date（buildMarketSessions → 风险报告）', () => {
  it('后端行里的 local_date 进入时效判定：昨天报价 + 今天日期 ⇒ stale', () => {
    const sessions = buildMarketSessions([backendStatusRow()])
    expect(sessions?.CN?.localDate).toBe(DAY)

    const report = buildPositionRiskReport(
      [
        {
          id: 1,
          account_id: 1,
          symbol: '600111',
          market: 'CN',
          current_price: 33,
          cost_price: 36.52,
          quantity: 100,
          defense_price: 34,
          quote_time: `${PREV} 09:31:00`,
          quote_date: PREV,
          daily_pnl_period: 'today',
        },
      ],
      { marketSessions: sessions },
    )
    expect(report.rows[0].defenseState).toBe('stale')
    expect(report.rows[0].staleReason).toBe('period')
    expect(report.breached).toBe(0)
  })

  it('后端没给 local_date（老网关）⇒ 时点未证实，同样不判破位', () => {
    const sessions = buildMarketSessions([backendStatusRow({ local_date: undefined })])
    const report = buildPositionRiskReport(
      [
        {
          id: 1,
          account_id: 1,
          symbol: '600111',
          market: 'CN',
          current_price: 33,
          cost_price: 36.52,
          quantity: 100,
          defense_price: 34,
          quote_time: `${DAY} 09:31:00`,
          quote_date: DAY,
          daily_pnl_period: 'today',
        },
      ],
      { marketSessions: sessions },
    )
    expect(report.rows[0].defenseState).toBe('stale')
    expect(report.rows[0].staleReason).toBe('period_unknown')
    expect(report.breached).toBe(0)
  })
})

describe('续期失败联动（rowFromPosition verified 闸门）', () => {
  it('verified=false ⇒ 有价有防线也不判破位：stale/unknown/refresh_failed', () => {
    const report = buildPositionRiskReport(
      [
        {
          id: 1,
          account_id: 1,
          symbol: '600111',
          market: 'CN',
          current_price: 33,
          cost_price: 36.52,
          quantity: 100,
          defense_price: 34,
          quote_time: `${DAY} 09:31:00`,
          quote_date: DAY,
          daily_pnl_period: 'today',
        },
      ],
      {
        marketSessions: { CN: { localTime: '09:31', localDate: DAY, status: 'trading' } },
        verified: false,
      },
    )
    const row = report.rows[0]
    expect(row.freshness).toBe('unknown')
    expect(row.staleReason).toBe('refresh_failed')
    expect(row.defenseState).toBe('stale')
    expect(report.breached).toBe(0)
    expect(report.staleUnverified).toBe(1)
  })
})
