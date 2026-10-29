/**
 * WS 帧 → 本地行情表（2026-10-29 F4 增补：源只给到「日」的证据也要认）。
 *
 * 旧规则只认 `quote_time`：帧里有时刻才算「价格与时刻同源的一对」。TQ 没有报时字段，
 * 后端用**同标的日线收盘价配对**证明了日期（`quote_date` + `quote_date_basis`），
 * 若这里只认时刻，TQ 的当日盈亏会在 WS 路径上被打回 `unknown`，而 20s 轮询那条路
 * 又是有日期的 —— 两条路自相矛盾。
 *
 * 本文件钉的口径：
 *  1. 帧带**任一**证据（时刻或日期）⇒ 价格/日期/期间/语义标签一起换，全是同一次抓取；
 *  2. 帧什么证据都没带 ⇒ 只有「价格与上一轮那一对逐位相同且涨跌幅也没新信息」才保留旧配对；
 *  3. 证据作废时连 `quote_time_kind` / `quote_date_basis` 一起清 —— 半条证据会让 UI 把
 *     "TQ 日线口径"标成"腾讯快照刷新"。
 */
import { describe, expect, it } from 'vitest'

import { applyQuoteTicks, expireQuoteSnapshots } from '../quote-ticks'
import type { QuoteSnapshot } from '../shared'

const targets = [{ symbol: '600519', market: 'CN' }]

const base: QuoteSnapshot = {
  current_price: 1354,
  change_pct: 1.75,
  quote_time: '2026-10-29T16:14:57+08:00',
  quote_date: '2026-10-29',
  quote_time_kind: 'snapshot_refresh',
  quote_date_basis: '',
  daily_pnl_period: 'today',
}

describe('applyQuoteTicks · 源证据', () => {
  it('帧只带日期（TQ）⇒ 价格与日期一起换，时刻明确为空', () => {
    const next = applyQuoteTicks({}, targets, {
      '600519': {
        price: 1354,
        change_pct: 1.75,
        quote_time: null,
        quote_date: '2026-10-29',
        quote_date_basis: 'daily_bar_close',
        daily_pnl_period: 'today',
      },
    })
    const row = next['CN:600519']
    expect(row.current_price).toBe(1354)
    expect(row.quote_time).toBeNull()
    expect(row.quote_date).toBe('2026-10-29')
    expect(row.quote_date_basis).toBe('daily_bar_close')
    expect(row.quote_time_kind).toBeNull()
    expect(row.daily_pnl_period).toBe('today')
  })

  it('帧带时刻 + 快照刷新语义 ⇒ 语义原样落到快照上', () => {
    const next = applyQuoteTicks({}, targets, {
      '600519': {
        price: 1354,
        change_pct: 1.75,
        quote_time: '2026-10-29T16:14:57+08:00',
        quote_date: '2026-10-29',
        quote_time_kind: 'snapshot_refresh',
        daily_pnl_period: 'today',
      },
    })
    expect(next['CN:600519'].quote_time_kind).toBe('snapshot_refresh')
  })

  it('帧没有任何证据、但价格逐位相同 ⇒ 旧配对（含日期/语义）保留', () => {
    const next = applyQuoteTicks({ 'CN:600519': base }, targets, {
      '600519': { price: 1354, change_pct: 1.75 },
    })
    expect(next['CN:600519'].quote_date).toBe('2026-10-29')
    expect(next['CN:600519'].quote_time_kind).toBe('snapshot_refresh')
    expect(next['CN:600519'].daily_pnl_period).toBe('today')
  })

  it('价格变了又没有新证据 ⇒ 全部作废（不许旧时刻替新价背书）', () => {
    const next = applyQuoteTicks({ 'CN:600519': base }, targets, {
      '600519': { price: 1360, change_pct: 1.75 },
    })
    const row = next['CN:600519']
    expect(row.current_price).toBe(1360)
    expect(row.quote_time).toBeNull()
    expect(row.quote_date).toBeNull()
    expect(row.quote_time_kind).toBeNull()
    expect(row.quote_date_basis).toBeNull()
    expect(row.daily_pnl_period).toBe('unknown')
  })

  it('价格没变但涨跌幅变了（隔日同价）⇒ 旧日期/期间不许给新涨跌幅作证', () => {
    const next = applyQuoteTicks({ 'CN:600519': base }, targets, {
      '600519': { price: 1354, change_pct: 0.5 },
    })
    expect(next['CN:600519'].quote_date).toBeNull()
    expect(next['CN:600519'].daily_pnl_period).toBe('unknown')
  })

  it('有效期到期：价留着陈列，时效证据（含语义标签）全部作废', () => {
    const next = expireQuoteSnapshots({ 'CN:600519': base })
    const row = next['CN:600519']
    expect(row.current_price).toBe(1354)
    expect(row.change_pct).toBeNull()
    expect(row.quote_time).toBeNull()
    expect(row.quote_date).toBeNull()
    expect(row.quote_time_kind).toBeNull()
    expect(row.quote_date_basis).toBeNull()
    expect(row.daily_pnl_period).toBe('unknown')
  })
})
