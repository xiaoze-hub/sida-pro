// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import FundStructurePanel, { fmtDay, type FundStructureResp } from '../FundStructurePanel'

/**
 * 四档资金结构面板 · 稀疏数据展示契约(specs/api/fund_structure.md EXDAY-003/005)。
 *
 * 表 exday_daily 2026-10-23 起才落库累积、历史无法回填 —— 消费端把"只有 1-2 天"
 * 画成"长期趋势"、或把缺日期补 0, 都是口径造假。本文件钉渲染侧行为:
 *   ① 口径徽章 tdx_exday4 + 累积标注恒显(与同屏逐笔/东财四档口径互不冒充);
 *   ② 单日只展示当日结构, 不渲染趋势序列(显式说明行);
 *   ③ 无数据/失败显式降级, 绝不编造。
 */

afterEach(cleanup)

function resp(over: Partial<FundStructureResp> = {}): FundStructureResp {
  return {
    available: true,
    symbol: '002361',
    market: 'CN',
    caliber: 'tdx_exday4',
    direction_semantics:
      'size_bucket_net(按单笔成交金额分四档的客户端口径, 非逐笔方向; 与腾讯逐笔(tick)/东财四档(eastmoney4)均不可换算, 禁止用于主力意图判定, 仅作资金面参考)',
    tier_thresholds_hint: '特大>100万 / 大20~100万 / 中5~20万 / 小<5万（推断阈值）',
    data_since: '20261023',
    count: 1,
    items: [
      {
        trade_date: '20261023',
        symbol: '002361',
        market: 'CN',
        amount: 5.48e8,
        active_buy_amt: 2.76e8,
        active_sell_amt: 2.72e8,
        active_ratio: 50.4,
        xl_buy_amt: 1.3e7,
        xl_sell_amt: 1.6e7,
        xl_active_net: -3.0e6,
        sm_buy_amt: 3.1e8,
        sm_sell_amt: 2.8e8,
        sm_active_net: 5.06e6,
        retail_ratio: 56.9,
        main_active_net_pct: 0.96,
        chip_migration: 2.2e5,
        quality: 'ok',
        source: 'tdx_tq_exday',
      },
    ],
    ...over,
  }
}

describe('FundStructurePanel 口径与稀疏标注', () => {
  it('口径徽章 tdx_exday4 与「数据自 2026-10-23 起累积 · N 个交易日」恒显', () => {
    render(<FundStructurePanel data={resp({ count: 2, items: [...resp().items!, { ...resp().items![0], trade_date: '20261024' }] })} loading={false} failed={false} lastOkAt="" />)
    expect(screen.getByTestId('fund-structure-caliber').textContent).toBe('通达信四档')
    // 2026-10-24 合并去重: 徽章改用共享 CaliberBadge(全站同一样式), 可见文案是
    // 「通达信四档」, 口径代码 tdx_exday4 在组件 tooltip 里(下面另断言语义仍在)。
    expect(screen.getByTestId('fund-structure-caliber').innerHTML).toMatch(/tdx_exday4|通达信/)
    const since = screen.getByTestId('fund-structure-since').textContent ?? ''
    expect(since).toContain('数据自 2026-10-23 起累积')
    expect(since).toContain('2 个交易日')
  })

  it('单日数据只展示当日结构, 不画趋势序列(显式说明行)', () => {
    render(<FundStructurePanel data={resp()} loading={false} failed={false} lastOkAt="" />)
    expect(screen.getByTestId('fund-structure-single-day').textContent).toContain('不画趋势')
    expect(screen.queryByTestId('fund-structure-series')).toBeNull()
    // 当日结构仍在: 四档行 + 特大单
    expect(screen.getByTestId('fund-structure-tiers')).not.toBeNull()
    expect(screen.getByText('特大单')).not.toBeNull()
  })

  it('≥2 个交易日才渲染序列柱带', () => {
    const two = resp({ count: 2, items: [resp().items![0], { ...resp().items![0], trade_date: '20261024' }] })
    render(<FundStructurePanel data={two} loading={false} failed={false} lastOkAt="" />)
    expect(screen.getByTestId('fund-structure-series')).not.toBeNull()
    expect(screen.getByTestId('fund-structure-band-active-ratio')).not.toBeNull()
    expect(screen.getByTestId('fund-structure-band-main-net')).not.toBeNull()
    expect(screen.queryByTestId('fund-structure-single-day')).toBeNull()
  })

  it('quality 非 ok 显式「数据待核」+ 后端原因原文', () => {
    const bad = resp({ items: [{ ...resp().items![0], quality: 'suspect', reason: '买卖合计不等' }] })
    render(<FundStructurePanel data={bad} loading={false} failed={false} lastOkAt="" />)
    expect(screen.getByTestId('fund-structure-quality').textContent).toContain('买卖合计不等')
  })
})

describe('FundStructurePanel 无数据与降级', () => {
  it('available=false 显式降级 + 后端 reason 原文, 绝不编造', () => {
    render(
      <FundStructurePanel
        data={{ available: false, reason: 'exday_daily 读取失败: OperationalError', caliber: 'tdx_exday4' }}
        loading={false}
        failed={false}
        lastOkAt=""
      />,
    )
    expect(screen.getByTestId('fund-structure-no-data').textContent).toContain('exday_daily 读取失败')
    expect(screen.queryByTestId('fund-structure-tiers')).toBeNull()
  })

  it('count=0(该股未落库)显式「无数据」, 累积起点照标', () => {
    render(<FundStructurePanel data={resp({ count: 0, items: [] })} loading={false} failed={false} lastOkAt="" />)
    const el = screen.getByTestId('fund-structure-no-data')
    expect(el.textContent).toContain('暂无落库记录')
    expect(el.textContent).toContain('数据自 2026-10-23 起累积')
  })

  it('取数失败(从未成功)显式「取数失败」, 不把失败当无数据', () => {
    render(<FundStructurePanel data={null} loading={false} failed lastOkAt="" />)
    expect(screen.getByTestId('fund-structure-no-data').textContent).toContain('取数失败（暂无成功记录）')
  })

  it('首帧加载中不闪「无数据」', () => {
    render(<FundStructurePanel data={null} loading failed={false} lastOkAt="" />)
    expect(screen.getByTestId('fund-structure-no-data').textContent).toContain('加载中…')
  })
})

describe('FundStructurePanel 纯函数', () => {
  it('fmtDay: YYYYMMDD → YYYY-MM-DD, 非格式原样返回', () => {
    expect(fmtDay('20261023')).toBe('2026-10-23')
    expect(fmtDay('2026-10-23')).toBe('2026-10-23')
    expect(fmtDay(null)).toBe('')
    expect(fmtDay('abc')).toBe('abc')
  })

  it('缺值档位渲染 -- 而不是 0', () => {
    const sparse = resp({
      items: [{ ...resp().items![0], xl_buy_amt: null, xl_sell_amt: null, xl_active_net: null }],
    })
    const { container } = render(<FundStructurePanel data={sparse} loading={false} failed={false} lastOkAt="" />)
    expect(container.querySelectorAll('[data-testid="fund-structure-tier-row"]').length).toBe(4)
    expect(container.textContent).toContain('--')
  })
})
