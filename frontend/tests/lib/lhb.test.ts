/**
 * 龙虎榜工作区纯函数契约(先红后绿)。
 *
 * 钉住四条用户可感的口径:
 *  ① 同一股票同日可有多条上榜原因 —— **分组但绝不加总金额**(东财每行是按原因披露的原始读数,
 *     盲加会重复计成交);
 *  ② 金额单位是**元**, 页面按 万/亿 换算显示, 不二次换算;
 *  ③ 买卖席位只按 side 字段切分, 不做"净额正=买方"这类猜测;
 *  ④ 明确区分"数据日期"与"今天"(源日期必须可追溯)。
 */
import { describe, expect, it } from 'vitest'

import {
  SEAT_IDENTITY_DISCLAIMER,
  formatTradeDate,
  groupLhbBySymbol,
  latestDragonTigerDate,
  reasonLabel,
  splitSeats,
} from '@/lib/lhb'

const row = (over: Partial<Record<string, unknown>> = {}) => ({
  trade_date: '20261106',
  symbol: '600000',
  market: 'CN',
  name: '浦发银行',
  reason: '日涨幅偏离值达7%',
  close: 10.5,
  change_pct: 8.1,
  net_buy: 120_000_000,
  buy_amt: 300_000_000,
  sell_amt: 180_000_000,
  deal_amt: 900_000_000,
  turnover_pct: 3.2,
  free_market_cap: 300_000_000_000,
  source: 'eastmoney',
  created_at: '2026-11-06T18:00:00',
  ...over,
})

describe('groupLhbBySymbol', () => {
  it('同股多条上榜原因 → 一个分组内保留全部原因, 不合并成一行', () => {
    const groups = groupLhbBySymbol([
      row({ reason: '日涨幅偏离值达7%', net_buy: 100_000_000 }),
      row({ reason: '连续三个交易日涨幅偏离值达20%', net_buy: 100_000_000 }),
    ])
    expect(groups).toHaveLength(1)
    expect(groups[0].symbol).toBe('600000')
    expect(groups[0].reasons.map((r) => r.reason)).toEqual([
      '日涨幅偏离值达7%',
      '连续三个交易日涨幅偏离值达20%',
    ])
    expect(groups[0].multipleReasons).toBe(true)
    // 关键: 金额是每原因原始行, 不在这里求和(100M + 100M = 200M 是重复计)
    expect(groups[0].reasons.map((r) => r.netBuy)).toEqual([100_000_000, 100_000_000])
  })

  it('按首次出现顺序分组(后端已是日期降序/代码升序), 不重新排序打乱榜单', () => {
    const groups = groupLhbBySymbol([
      row({ symbol: '000001', name: '平安银行' }),
      row({ symbol: '600000' }),
      row({ symbol: '000001', name: '平安银行', reason: '日换手率达20%' }),
    ])
    expect(groups.map((g) => g.symbol)).toEqual(['000001', '600000'])
    expect(groups[0].reasons).toHaveLength(2)
  })

  it('同一原因重复出现只保留一条(唯一键兜底), 不重复展示', () => {
    const groups = groupLhbBySymbol([row(), row({ net_buy: 1 })])
    expect(groups[0].reasons).toHaveLength(1)
  })

  it('缺失代码/空行被丢弃, 不产出幽灵分组', () => {
    expect(groupLhbBySymbol([])).toEqual([])
    expect(groupLhbBySymbol([row({ symbol: '' })])).toEqual([])
  })
})

describe('金额与原因口径', () => {
  it('金额字段原样保留为元(不做 /1e4 二次换算)', () => {
    const [g] = groupLhbBySymbol([row({ buy_amt: 5_000_000 })])
    expect(g.reasons[0].buyAmt).toBe(5_000_000)
  })

  it('空原因给统一占位文案, 不渲染空白单元格', () => {
    expect(reasonLabel('')).toBe('未注明上榜原因')
    expect(reasonLabel('  ')).toBe('未注明上榜原因')
    expect(reasonLabel('日涨幅偏离值达7%')).toBe('日涨幅偏离值达7%')
  })
})

describe('splitSeats', () => {
  const seat = (over: Partial<Record<string, unknown>> = {}) => ({
    row_uid: 'u1',
    trade_date: '20261106',
    side: 'buy',
    symbol: '600000',
    name: '浦发银行',
    reason: '日涨幅偏离值达7%',
    operate_dept_code: '1001',
    operate_dept_name: '某某证券股份有限公司上海某营业部',
    close: 10.5,
    change_pct: 8.1,
    accum_amount: 900_000_000,
    accum_volume: 0,
    buy_amt: 50_000_000,
    sell_amt: 0,
    net_amt: 50_000_000,
    rise_probability_3d: 55.5,
    change_type: '1',
    trade_id: 't1',
    total_buy_ratio: 5.5,
    total_sell_ratio: 0,
    source: 'eastmoney',
    created_at: '2026-11-06T18:00:00',
    ...over,
  })

  it('按 side 字段切分买卖, 不按净额符号猜', () => {
    const out = splitSeats([
      seat({ row_uid: 'b1', side: 'buy', net_amt: 10 }),
      seat({ row_uid: 's1', side: 'sell', net_amt: 99 }),
      // side=sell 但净额为正(盘中回补) —— 仍必须留在卖方
      seat({ row_uid: 's2', side: 'sell', net_amt: 5 }),
    ])
    expect(out.buy.map((s) => s.row_uid)).toEqual(['b1'])
    expect(out.sell.map((s) => s.row_uid)).toEqual(['s1', 's2'])
  })

  it('买方按净额降序, 空净额排最后(不当作 0 领先)', () => {
    const out = splitSeats([
      seat({ row_uid: 'n1', net_amt: null }),
      seat({ row_uid: 'n2', net_amt: 20 }),
      seat({ row_uid: 'n3', net_amt: 80 }),
    ])
    expect(out.buy.map((s) => s.row_uid)).toEqual(['n3', 'n2', 'n1'])
  })

  it('row_uid 去重(同股同原因同席位重复拉取不重复列)', () => {
    const out = splitSeats([seat({ row_uid: 'x' }), seat({ row_uid: 'x', buy_amt: 1 })])
    expect(out.buy).toHaveLength(1)
  })

  it('未知 side 丢弃, 不硬塞进买方', () => {
    const out = splitSeats([seat({ side: 'unknown' })])
    expect(out.buy).toHaveLength(0)
    expect(out.sell).toHaveLength(0)
  })

  it('营业部名缺失给占位, 不编造机构名', () => {
    const out = splitSeats([seat({ operate_dept_name: '' })])
    expect(out.buy[0].deptName).toBe('未披露营业部')
  })
})

describe('来源日期', () => {
  it('YYYYMMDD → 2026-11-06(展示层可读), 脏值 → --', () => {
    expect(formatTradeDate('20261106')).toBe('2026-11-06')
    expect(formatTradeDate('2026-11-06')).toBe('2026-11-06')
    expect(formatTradeDate(null)).toBe('--')
    expect(formatTradeDate('2026')).toBe('--')
  })

  it('overview 里取 dragon_tiger_events 最新日期; 缺失/统计失败 → null(不猜今天)', () => {
    const tables = [
      { table: 'quote_snapshots', latest_date: '20261107' },
      { table: 'dragon_tiger_events', latest_date: '20261106' },
    ]
    expect(latestDragonTigerDate(tables)).toBe('20261106')
    expect(latestDragonTigerDate([{ table: 'dragon_tiger_events', latest_date: null }])).toBeNull()
    expect(latestDragonTigerDate([])).toBeNull()
    expect(latestDragonTigerDate(undefined)).toBeNull()
  })

  it('营业部身份免责声明不能被悄悄删掉', () => {
    expect(SEAT_IDENTITY_DISCLAIMER).toContain('营业部席位')
    expect(SEAT_IDENTITY_DISCLAIMER).toContain('不等于已验证游资')
  })
})
