/**
 * 龙虎榜 · 席位身份口径(先红后绿, 2026-11-09 复审第二轮)。
 *
 * 复审指出的两处**误导**:
 *  ① `seatSummary` 按营业部**名称**去重, 把 3 条「机构专用」算成 1 家 —— 但「机构专用」是
 *     交易所披露里的共用称号, 背后是**多家不同机构**; 合并等于凭空断言"只有一家"。
 *     同理, 名称/代码都缺的行, 之前"各算一家"也是凭空断言"是不同家"。
 *     口径改为: 身份能核实(具体营业部名或席位代码, 且无同名多码冲突)才报家数;
 *     核实不了**只报源记录条数 + 身份未知**, 不推测独立家数。
 *     判定只用身份证据(名称/代码), **绝不用金额相等推同一家**。
 *  ② 北交所 920002 与 ETF/可转债一样不满足参谋入口的锚点正则, 旧文案统一写
 *     "属 ETF/可转债等非股票代码" —— 把北交所股票说成了非股票。改为**不猜证券类型**
 *     的中性文案: 只说入口没把它识别为股票锚点、代码只在问题正文里。
 */
import { describe, expect, it } from 'vitest'

import {
  SEAT_IDENTITY_UNKNOWN_NOTE,
  lhbCopilotHandoff,
  seatCountText,
  seatSummary,
  splitSeats,
} from '@/lib/lhb'

const seat = (over: Record<string, unknown> = {}) => ({
  row_uid: 'r1',
  trade_date: '20261106',
  side: 'buy',
  symbol: '920002',
  name: '万达轴承',
  reason: '日涨幅偏离值达7%',
  operate_dept_code: '1001',
  operate_dept_name: '某某证券股份有限公司上海某营业部',
  buy_amt: 10_000_000,
  sell_amt: 0,
  net_amt: 10_000_000,
  ...over,
})

const summaryOf = (rows: NonNullable<Parameters<typeof splitSeats>[0]>) => seatSummary(splitSeats(rows).buy)

describe('席位身份: 核实不了就只报条数, 不推测独立家数', () => {
  it('3 条「机构专用」不再算成 1 家营业部', () => {
    const s = summaryOf([
      seat({ row_uid: 'a', operate_dept_name: '机构专用' }),
      seat({ row_uid: 'b', operate_dept_name: '机构专用' }),
      seat({ row_uid: 'c', operate_dept_name: '机构专用' }),
    ])
    expect(s.rows).toBe(3)
    expect(s.identityVerified).toBe(false)
    expect(s.unknownIdentityRows).toBe(3)
    // 未核实 → departments 不是可展示的家数; 页面文案必须只说条数
    expect(seatCountText(s)).toContain('3 条记录')
    expect(seatCountText(s)).toContain('身份未知')
    expect(seatCountText(s)).not.toContain('家营业部')
    expect(seatCountText(s)).not.toContain('1 家')
  })

  it('「机构专用」即使带不同代码也不算可核实的家数(称号本身不指向具体机构)', () => {
    const s = summaryOf([
      seat({ row_uid: 'a', operate_dept_name: '机构专用', operate_dept_code: '9001' }),
      seat({ row_uid: 'b', operate_dept_name: '机构专用', operate_dept_code: '9002' }),
    ])
    expect(s.rows).toBe(2)
    expect(s.identityVerified).toBe(false)
    expect(s.unknownIdentityRows).toBe(2)
    expect(seatCountText(s)).not.toContain('家营业部')
  })

  it('真营业部 + 机构专用混排: 整侧不报家数(不能把未知行悄悄排除后宣称总数)', () => {
    const s = summaryOf([
      seat({ row_uid: 'a', operate_dept_code: '1001', operate_dept_name: '甲营业部' }),
      seat({ row_uid: 'b', operate_dept_code: '1002', operate_dept_name: '乙营业部' }),
      seat({ row_uid: 'c', operate_dept_code: '9001', operate_dept_name: '机构专用' }),
    ])
    expect(s.rows).toBe(3)
    expect(s.identityVerified).toBe(false)
    expect(s.unknownIdentityRows).toBe(1)
    const text = seatCountText(s)
    expect(text).toContain('3 条记录')
    expect(text).toContain('身份未知')
    expect(text).not.toContain('2 家')
  })

  it('名称与代码都缺的行: 不能断言彼此不同, 只算未知(之前各算一家的口径作废)', () => {
    const s = summaryOf([
      seat({ row_uid: 'a', operate_dept_code: '', operate_dept_name: '' }),
      seat({ row_uid: 'b', operate_dept_code: '', operate_dept_name: '' }),
    ])
    expect(s.rows).toBe(2)
    expect(s.identityVerified).toBe(false)
    expect(s.unknownIdentityRows).toBe(2)
    expect(seatCountText(s)).toContain('身份未知')
  })

  it('身份都能核实(具体营业部代码)才报家数, 同码多原因行仍合为 1 家', () => {
    const s = summaryOf([
      seat({ row_uid: 'a', reason: '日涨幅偏离值达7%' }),
      seat({ row_uid: 'b', reason: '连续三个交易日涨幅偏离值达20%' }),
    ])
    expect(s.rows).toBe(2)
    expect(s.departments).toBe(1)
    expect(s.identityVerified).toBe(true)
    expect(s.unknownIdentityRows).toBe(0)
    expect(seatCountText(s)).toContain('2 条记录')
    expect(seatCountText(s)).toContain('1 家营业部')
  })

  it('同名但不同席位代码 → 称不足以区分, 只报条数不报家数(不猜是 1 家还是 2 家)', () => {
    const s = summaryOf([
      seat({ row_uid: 'a', operate_dept_code: '1001', operate_dept_name: '某某证券上海营业部' }),
      seat({ row_uid: 'b', operate_dept_code: '1002', operate_dept_name: '某某证券上海营业部' }),
    ])
    expect(s.rows).toBe(2)
    expect(s.identityVerified).toBe(false)
    expect(seatCountText(s)).toContain('2 条记录')
    expect(seatCountText(s)).toContain('身份未知')
    expect(seatCountText(s)).not.toContain('家营业部')
  })

  it('没有席位代码但有具体营业部全名 → 按名称核实, 同名合为 1 家', () => {
    const s = summaryOf([
      seat({ row_uid: 'a', operate_dept_code: '', operate_dept_name: '某某证券股份有限公司杭州文三路营业部' }),
      seat({ row_uid: 'b', operate_dept_code: '', operate_dept_name: '某某证券股份有限公司杭州文三路营业部' }),
    ])
    expect(s.rows).toBe(2)
    expect(s.departments).toBe(1)
    expect(s.identityVerified).toBe(true)
  })

  it('金额完全相同不是身份证据: 不同名称的行不得因金额相等被合并', () => {
    const same = { buy_amt: 12_345_600, sell_amt: 0, net_amt: 12_345_600 }
    const s = summaryOf([
      seat({ row_uid: 'a', operate_dept_code: '1001', operate_dept_name: '甲营业部', ...same }),
      seat({ row_uid: 'b', operate_dept_code: '1002', operate_dept_name: '乙营业部', ...same }),
    ])
    expect(s.rows).toBe(2)
    expect(s.departments).toBe(2)
    expect(s.identityVerified).toBe(true)
  })

  it('空列表: 只写 0 条, 不出 NaN / 不写家数', () => {
    const s = seatSummary([])
    expect(s.rows).toBe(0)
    expect(s.unknownIdentityRows).toBe(0)
    expect(seatCountText(s)).toBe('0 条记录')
  })

  it('身份未知的说明文案点明「机构专用」且不写家数', () => {
    expect(SEAT_IDENTITY_UNKNOWN_NOTE).toContain('机构专用')
    expect(SEAT_IDENTITY_UNKNOWN_NOTE).toContain('不')
    expect(SEAT_IDENTITY_UNKNOWN_NOTE).not.toMatch(/\d+\s*家/)
  })
})

describe('参谋交接: 无法设锚点时用不猜证券类型的文案', () => {
  it('北交所 920002 不得被说成非股票/ETF/可转债', () => {
    const h = lhbCopilotHandoff({ symbol: '920002', name: '万达轴承', date: '20261106' })
    expect(h.anchor).toBe(false)
    expect(h.url).not.toBe('')
    expect(h.anchorNote).toContain('920002')
    expect(h.anchorNote).not.toContain('非股票')
    expect(h.anchorNote).not.toContain('ETF')
    expect(h.anchorNote).not.toContain('可转债')
    // 不猜类型, 只说入口没识别为锚点
    expect(h.anchorNote).toContain('锚点')
  })

  it('ETF 510300 / 可转债 113050 同样走中性文案(不反向猜类型)', () => {
    for (const code of ['510300', '113050']) {
      const h = lhbCopilotHandoff({ symbol: code, date: '20261106' })
      expect(h.anchor).toBe(false)
      expect(h.anchorNote).toContain(code)
      expect(h.anchorNote).not.toContain('非股票')
      expect(h.anchorNote).not.toContain('ETF')
      expect(h.anchorNote).not.toContain('可转债')
    }
  })

  it('A 股 600000: 有锚点, 不需要额外说明', () => {
    const h = lhbCopilotHandoff({ symbol: '600000', name: '浦发银行', date: '20261106' })
    expect(h.anchor).toBe(true)
    expect(h.anchorNote).toBe('')
  })
})
