/**
 * 龙虎榜工作区 · 前端复审契约(先红后绿, 2026-11-09)。
 *
 * 复审提出的四件事, 都是**用户会被误导**的口径问题, 不是文案洁癖:
 *  ① 后端已经给出 `quality`(缺口/风险定性), 前端必须真的消费并展示; 拿不到时
 *     **保持未知**, 不许因为"有行"就说完整, 更不许把行数当完整性凭据;
 *  ② 参谋入口只把 6 位 A 股代码当锚点(`/^[03468]\d{5}$/`, 见 AiCopilot SYMBOL_RE)。
 *     ETF(5xxxxx)/可转债(1xxxxx) 带 `?symbol=` 进去**不会**变成锚点 —— 交接时
 *     不能承诺一个不会出现的锚点, 只能把代码写进问题正文;
 *  ③ 入口只**预填**问题, 不自动发送 —— 文案必须说清;
 *  ④ `lhb_seat_details` 一个营业部可在多条上榜原因下各占一行, **行数 ≠ 独立席位数**,
 *     不能把 2 行同营业部说成"2 席"。
 */
import { describe, expect, it } from 'vitest'

import {
  LHB_DAY_LIMIT,
  buildLhbCopilotUrl,
  lhbCopilotHandoff,
  lhbCoverage,
  lhbQualityView,
  seatCountText,
  seatSummary,
  splitSeats,
} from '@/lib/lhb'

const quality = (over: Record<string, unknown> = {}) => ({
  table: 'dragon_tiger_events',
  trade_date: '20261106',
  market: 'CN',
  status: 'unverified',
  complete: false,
  degraded: false,
  verification: 'unverified',
  severity: 'caution',
  label: '未经核对',
  completeness_claimed: false,
  verified_against_official: false,
  actual_symbols: 2,
  first_write_at: '2026-11-06 15:02:11',
  last_write_at: '2026-11-06 15:04:03',
  publish_ready_after: '16:00',
  publish_ready_basis: '经验下界',
  early_freeze: true,
  early_freeze_risk: true,
  early_freeze_note: '早写风险: created_at 最早 2026-11-06 15:02 早于经验下界 16:00(北京)。',
  cross_check: {
    daily_symbols: 3,
    only_in_events: 1,
    only_in_daily: 2,
    note: '双向差异仅作交叉提示: 两表均非官方完整清单, 不能据此判定任何一方完整',
  },
  warnings: ['early_freeze_risk: 最早写入 2026-11-06 15:02 早于发布就绪经验下界 16:00, 风险提示。'],
  note: 'dragon_tiger_events 为定时回源落库的独立来源, 无官方完整清单可核对: 不声明完整, 也无证据表明残缺。',
  ...over,
})

describe('quality 定性: 接收后端真实缺口与风险', () => {
  it('没给 quality / 空对象 / 无 status → 未知, 不猜完整也不猜残缺', () => {
    for (const raw of [undefined, null, {}, { status: '' }, 'unverified']) {
      const v = lhbQualityView(raw)
      expect(v.known).toBe(false)
      expect(v.label).toBe('覆盖未核实')
      expect(v.text).toContain('未知')
      expect(v.text).not.toContain('已核对完整')
      expect(v.risks).toEqual([])
      expect(v.crossCheck).toBe('')
    }
  })

  it('unverified: 展示后端定性/风险/交叉提示, 且永不输出完整性承诺', () => {
    const v = lhbQualityView(quality())
    expect(v.known).toBe(true)
    expect(v.severity).toBe('caution')
    expect(v.label).toBe('未经核对')
    expect(v.text).toContain('不声明完整')
    // 早写风险必须作为风险展示(不是被吞掉的 note)
    expect(v.risks.join(' ')).toContain('最早写入')
    expect(v.risks.join(' ')).toContain('风险提示')
    expect(v.crossCheck).toContain('不能据此判定任何一方完整')
    expect(v.crossCheck).toContain('多 1 只')
    expect(v.crossCheck).toContain('少 2 只')
    expect(v.writeWindow).toContain('15:02')
    expect(v.writeWindow).toContain('16:00')
    expect(v.completenessClaim).toBe(false)
  })

  it('后端声称 complete 但未经官方核对 → 不采信, 页面不得写「已核对完整」', () => {
    const v = lhbQualityView(
      quality({ status: 'complete', complete: true, completeness_claimed: true, label: '完整' }),
    )
    expect(v.label).not.toBe('已核对完整')
    expect(v.label).not.toBe('完整')
    expect(v.text).toContain('不采信')
    expect(v.completenessClaim).toBe(false)
  })

  it('只有 verified_against_official=true 才允许说已核对完整', () => {
    const v = lhbQualityView(
      quality({ status: 'complete', complete: true, completeness_claimed: true, verified_against_official: true, label: '完整' }),
    )
    expect(v.label).toBe('已核对完整')
    expect(v.completenessClaim).toBe(true)
  })

  it('absent: 无快照要说清「无覆盖 ≠ 当日没有龙虎榜」', () => {
    const v = lhbQualityView(quality({ status: 'absent', severity: 'missing', degraded: true, label: '无快照', actual_symbols: 0 }))
    expect(v.known).toBe(true)
    expect(v.severity).toBe('missing')
    expect(v.text).toContain('无快照')
    expect(v.text).toContain('不等于')
  })

  it('市场口径只覆盖部分行时, 明说未覆盖其余市场(不推断其它市场完整/残缺)', () => {
    const v = lhbQualityView(
      quality({ rows_total: 9, rows_in_market: 6, market_scope_note: '本次定性仅覆盖 CN 市场 6/9 行; 其余市场未参与定性。' }),
    )
    expect(v.text).toContain('仅覆盖 CN 市场 6/9 行')
  })
})

describe('覆盖口径: 截断事实 + 质量定性合并, 不加总成完整性结论', () => {
  it('未触达上限时用后端 quality 文案, 行数只作「已入库条数」', () => {
    const c = lhbCoverage(37, LHB_DAY_LIMIT, quality())
    expect(c.truncated).toBe(false)
    expect(c.text).toContain('37')
    expect(c.text).toContain('不声明完整')
    expect(c.text).not.toContain('完整榜单')
  })

  it('没有 quality 时保持未知, 不因有行就暗示完整', () => {
    const c = lhbCoverage(37)
    expect(c.text).toContain('未知')
    expect(c.text).not.toContain('完整榜单')
  })

  it('触达单次上限: 明确可能被截断(即使后端定性为 unverified)', () => {
    const c = lhbCoverage(LHB_DAY_LIMIT, LHB_DAY_LIMIT, quality())
    expect(c.truncated).toBe(true)
    expect(c.text).toContain('截断')
    expect(c.text).toContain(String(LHB_DAY_LIMIT))
  })

  it('空榜 + absent 定性: 无覆盖不等于当日没有龙虎榜', () => {
    const c = lhbCoverage(0, LHB_DAY_LIMIT, quality({ status: 'absent', severity: 'missing', label: '无快照' }))
    expect(c.text).toContain('无快照')
    expect(c.text).toContain('不等于')
  })
})

describe('席位条数 ≠ 独立席位(同营业部可因多原因多行)', () => {
  const seat = (over: Record<string, unknown> = {}) => ({
    row_uid: 'r1',
    trade_date: '20261106',
    side: 'buy',
    symbol: '600000',
    name: '浦发银行',
    reason: '日涨幅偏离值达7%',
    operate_dept_code: '1001',
    operate_dept_name: '某某证券上海某营业部',
    buy_amt: 10,
    sell_amt: 0,
    net_amt: 10,
    ...over,
  })

  it('同一营业部两条原因行 → 2 条记录 / 1 家营业部, 且标记多原因', () => {
    const { buy } = splitSeats([
      seat({ row_uid: 'a', reason: '日涨幅偏离值达7%' }),
      seat({ row_uid: 'b', reason: '连续三个交易日涨幅偏离值达20%' }),
    ])
    const s = seatSummary(buy)
    expect(s.rows).toBe(2)
    expect(s.departments).toBe(1)
    expect(s.multipleReasons).toBe(true)
  })

  it('不同营业部 → 独立家数按营业部计, 不按行计', () => {
    const { buy } = splitSeats([
      seat({ row_uid: 'a', operate_dept_code: '1001', operate_dept_name: '甲营业部' }),
      seat({ row_uid: 'b', operate_dept_code: '1002', operate_dept_name: '乙营业部' }),
      seat({ row_uid: 'c', operate_dept_code: '1002', operate_dept_name: '乙营业部', reason: '日换手率达20%' }),
    ])
    const s = seatSummary(buy)
    expect(s.rows).toBe(3)
    expect(s.departments).toBe(2)
    expect(s.multipleReasons).toBe(true)
  })

  it('营业部名缺失的行: 没有代码/名称证据, 只能算身份未知, 不得"各算一家"', () => {
    const { buy } = splitSeats([
      seat({ row_uid: 'a', operate_dept_code: '', operate_dept_name: '', reason: '日涨幅偏离值达7%' }),
      seat({ row_uid: 'b', operate_dept_code: '', operate_dept_name: '', reason: '日换手率达20%' }),
    ])
    const s = seatSummary(buy)
    expect(s.rows).toBe(2)
    expect(s.identityVerified).toBe(false)
    expect(s.unknownIdentityRows).toBe(2)
    expect(seatCountText(s)).toContain('身份未知')
    expect(seatCountText(s)).not.toContain('家营业部')
  })

  it('空列表 → 0 条记录, 不显示 NaN, 不写家数', () => {
    expect(seatSummary([])).toEqual({
      rows: 0,
      departments: 0,
      identityVerified: true,
      unknownIdentityRows: 0,
      multipleReasons: false,
    })
    expect(seatCountText(seatSummary([]))).toBe('0 条记录')
  })
})

describe('参谋交接: 锚点只对 A 股代码承诺', () => {
  const parse = (url: string) => new URL(url, 'http://local.test')

  it('A 股 600000 → 带 symbol 锚点', () => {
    const h = lhbCopilotHandoff({ symbol: '600000', name: '浦发银行', date: '20261106' })
    expect(h.anchor).toBe(true)
    expect(parse(h.url).searchParams.get('symbol')).toBe('600000')
  })

  it('ETF 510300 / 可转债 113050 → 不承诺锚点, 不传 symbol, 代码只在问题正文', () => {
    for (const code of ['510300', '159915', '113050', '123118']) {
      const h = lhbCopilotHandoff({ symbol: code, name: '测试标的', date: '20261106' })
      expect(h.anchor).toBe(false)
      expect(h.url).not.toBe('')
      const u = parse(h.url)
      expect(u.pathname).toBe('/ai-copilot')
      expect(u.searchParams.has('symbol')).toBe(false)
      expect(u.searchParams.get('q')).toContain(code)
      // 不硬塞配方
      expect(u.searchParams.has('agent')).toBe(false)
    }
  })

  it('buildLhbCopilotUrl 对非股票代码同样不带 symbol(单一实现, 不两套口径)', () => {
    const u = parse(buildLhbCopilotUrl({ symbol: '510300', date: '20261106' }))
    expect(u.searchParams.has('symbol')).toBe(false)
    expect(u.searchParams.get('q')).toContain('510300')
  })
})
