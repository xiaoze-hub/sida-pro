import { describe, it, expect } from 'vitest'
import { mapSnapshot, type SnapshotCell } from '@panwatch/biz-ui/components/workbench/HeaderBand'

/**
 * 带1 快照行纯函数单测(任务3)。
 *
 * 合成对象的 key 一律用**后端真实返回字段名**(非计划书示意的 total_mv/amount/current_price 旧名):
 *  - GET /quotes/{symbol}          → src/web/api/quotes.py::_quote_to_response
 *      current_price / change_pct / open_price / high_price / low_price / turnover(成交额, 元)
 *  - GET /quotes/{symbol}/more-info → src/core/marketdata_client.py::md_more_info
 *      turnover_rate(换手率%) / volume_ratio(量比) / total_market_value(总市值, 亿)
 *  - GET /stocks/{symbol}/l2        → src/core/stock_l2.py::fetch_more
 *      more.zt_price(涨停价; Ruling A: HeaderBand 自取注入)
 * 换手率/量比/市值取自 more-info(与 quote 同名时以 more-info 为准)。
 */

const byKey = (rows: SnapshotCell[]): Record<string, string> =>
  Object.fromEntries(rows.map((r) => [r.key, r.value]))

describe('mapSnapshot(带1 快照行)', () => {
  it('缺值一律 --, 不编(空对象与 undefined 双跑)', () => {
    const b = byKey(mapSnapshot({}, {}))
    expect(b.price).toBe('--')
    expect(b.change_pct).toBe('--')
    expect(b.open).toBe('--')
    expect(b.high).toBe('--')
    expect(b.low).toBe('--')
    expect(b.amount).toBe('--')
    expect(b.turnover).toBe('--')
    expect(b.volume_ratio).toBe('--')
    expect(b.market_cap).toBe('--')
    expect(b.limit_price).toBe('--')

    expect(() => mapSnapshot(undefined, undefined)).not.toThrow()
    const u = byKey(mapSnapshot(undefined, undefined))
    expect(u.price).toBe('--')
    expect(u.turnover).toBe('--')
    expect(u.limit_price).toBe('--')
  })

  it('行情字段用真实 key: current_price/change_pct/open_price/high_price/low_price/turnover', () => {
    const b = byKey(
      mapSnapshot(
        {
          current_price: 82.46,
          change_pct: 7.86,
          open_price: 77.0,
          high_price: 83.2,
          low_price: 76.5,
          turnover: 123456789,
        },
        {},
      ),
    )
    expect(b.price).toBe('82.46')
    expect(b.change_pct).toBe('+7.86%')
    expect(b.open).toBe('77')
    expect(b.high).toBe('83.2')
    expect(b.low).toBe('76.5')
    // 成交额: more-info 无成交额字段, 取 quote.turnover(元) 经 fmtAmount → 亿/万
    expect(b.amount).toBe('1.23亿')
  })

  it('跌时带 - 号(涨跌幅符号不丢)', () => {
    expect(byKey(mapSnapshot({ current_price: 10, change_pct: -3.5 }, {})).change_pct).toBe('-3.50%')
    expect(byKey(mapSnapshot({ current_price: 10, change_pct: 0 }, {})).change_pct).toBe('+0.00%')
  })

  it('more-info 真实 key 落位: turnover_rate/volume_ratio/total_market_value(亿)', () => {
    const b = byKey(
      mapSnapshot(
        { current_price: 10 },
        { turnover_rate: 3.2, volume_ratio: 1.4, total_market_value: 1234.56 },
      ),
    )
    expect(b.turnover).toBe('3.2%')
    expect(b.volume_ratio).toBe('1.4')
    expect(b.market_cap).toBe('1234.56亿')
  })

  it('涨停价取 /stocks/{s}/l2 的 more.zt_price(Ruling A 自包含)', () => {
    expect(byKey(mapSnapshot({}, { zt_price: 11.22 })).limit_price).toBe('11.22')
  })

  it('PG DECIMAL→JSON 字符串数字不崩、不渲染 NaN', () => {
    const b = byKey(
      mapSnapshot(
        { current_price: '82.46', change_pct: '7.86', turnover: '123456789' },
        { turnover_rate: '3.2', volume_ratio: '1.4', total_market_value: '1234.56', zt_price: '11.22' },
      ),
    )
    expect(b.price).toBe('82.46')
    expect(b.change_pct).toBe('+7.86%')
    expect(b.amount).toBe('1.23亿')
    expect(b.turnover).toBe('3.2%')
    expect(b.market_cap).toBe('1234.56亿')
    expect(b.limit_price).toBe('11.22')
  })

  it('脏值(NaN/非数字串)走 -- 而非 NaN 文案', () => {
    const b = byKey(mapSnapshot({ current_price: 'abc', change_pct: NaN }, { total_market_value: 'x' }))
    expect(b.price).toBe('--')
    expect(b.change_pct).toBe('--')
    expect(b.market_cap).toBe('--')
  })

  it('cell 顺序与 key/label 固定(防漂移: 现价/涨跌幅/今开/最高/最低/成交额/换手率/量比/总市值/涨停价)', () => {
    const rows = mapSnapshot({}, {})
    expect(rows.map((r) => r.key)).toEqual([
      'price',
      'change_pct',
      'open',
      'high',
      'low',
      'amount',
      'turnover',
      'volume_ratio',
      'market_cap',
      'limit_price',
    ])
    expect(rows.every((r) => r.label.length > 0)).toBe(true)
  })
})
