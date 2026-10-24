// @vitest-environment jsdom
/**
 * A-03 / A-12 / A-13 前端回归(2026-10-18):
 *
 * 缺陷: 表头原来渲染 `09:25 集合竞价异动池 · 同步 {new Date()}` —— **浏览器本地时钟**。
 * 周日打开会把上周五 9:25 的池子说成"刚刚同步"。
 *
 * 本文件锁死:
 * ① 非实时 ⇒ 显示"数据日 <后端 data_date>"与阶段徽章, **不得出现"同步 HH:MM:SS"**;
 * ② 实时窗口 ⇒ 显示源端时钟(data_clock), 而不是浏览器时间;
 * ③ 空池 ⇒ 按后端 `reason_text` 给出可诊断文案(数据源不可用 ≠ 今日无异常);
 * ④ 表头合规: "盘口意图判定", 不出现"买卖建议"。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: mocks.fetchAPI,
  getToken: () => '',
  stocksApi: { create: vi.fn() },
}))

import AuctionAnomalyTab from '../../packages/biz-ui/src/components/AuctionAnomalyTab'

const RECORD = {
  symbol: '600111',
  code: '600111',
  name: '北方稀土',
  gap_pct: 3.35,
  withdraw_rate: 62.0,
  volume_ratio: 4.2,
  open_amount: 32_000_000,
  verdict_tag: '🔥 爆量抢筹',
  verdict_action: '主力资金超预期高开抢筹，量能充沛',
  verdict_level: 'buy',
}

const BASE = {
  available: true,
  count: 1,
  records: [RECORD],
  missing_fields: ['volume_ratio'],
  note: 'thsdk 竞价异动数据源不提供量比,字段固定为空',
  data_date: '2026-10-16',
  trade_phase: 'post_close',
  phase_label: '收盘后',
  is_trading_day: false,
  is_live: false,
  is_today: false,
  as_of: '2026-10-18T15:03:12+08:00',
  data_clock: '09:25:03',
  data_synced_at: '2026-10-16T09:25:03',
  reason: null,
  reason_text: '非实时：当前收盘后，展示 2026-10-16 的竞价快照',
}

function mockOnce(payload: unknown) {
  mocks.fetchAPI.mockImplementation((url: string) => {
    if (url.startsWith('/gap-study')) return Promise.resolve({ available: false })
    return Promise.resolve(payload)
  })
}

afterEach(() => {
  cleanup()
  mocks.fetchAPI.mockReset()
})

describe('AuctionAnomalyTab 数据日标注(A-03)', () => {
  it('非实时: 显示数据日与阶段, 不出现浏览器"同步"时钟', async () => {
    mockOnce(BASE)
    render(<AuctionAnomalyTab market="CN" />)

    expect(await screen.findByText('600111')).toBeTruthy()
    expect(screen.getByText('2026-10-16')).toBeTruthy()
    expect(screen.getByText(/收盘后/)).toBeTruthy()
    // 旧实现的关键证据: "同步" + 浏览器时间
    expect(screen.queryByText(/同步/)).toBeNull()
    // 合规表头
    expect(screen.getByText('9:25 盘口意图判定')).toBeTruthy()
    expect(screen.queryByText(/买卖建议/)).toBeNull()
  })

  it('实时窗口: 显示源端时钟而不是浏览器时间', async () => {
    mockOnce({ ...BASE, is_live: true, is_today: true, trade_phase: 'auction', phase_label: '集合竞价 09:15-09:25' })
    render(<AuctionAnomalyTab market="CN" />)

    expect(await screen.findByText('600111')).toBeTruthy()
    expect(screen.getByText('实时')).toBeTruthy()
    expect(screen.getByText('09:25:03')).toBeTruthy()
  })

  it('空池按 reason 分支: 数据源不可用 ≠ 今日无异常', async () => {
    mockOnce({
      ...BASE,
      available: false,
      count: 0,
      records: [],
      reason: 'source_unavailable',
      reason_text: '数据源不可用（thsdk 竞价异动拉取失败），非「今日无异常」',
    })
    render(<AuctionAnomalyTab market="CN" />)
    expect(await screen.findByText(/数据源不可用/)).toBeTruthy()
    expect(screen.queryByText(/非交易时段 \/ 数据源未接入/)).toBeNull()
  })

  it('空池按 reason 分支: 非交易日', async () => {
    mockOnce({
      ...BASE,
      available: false,
      count: 0,
      records: [],
      reason: 'non_trading_day',
      reason_text: '非交易日，竞价池无当日数据',
    })
    render(<AuctionAnomalyTab market="CN" />)
    expect(await screen.findByText(/非交易日/)).toBeTruthy()
  })

  /**
   * 2026-10-24: 首屏冷查询实测 **31 秒**（`/api/auction/anomaly` 冷缓存，
   * 实测 31477ms → 7280ms → 145ms）。期间 `data=null`、`total=0`，
   * 旧实现把它渲染成「共 0 只」，用户读作「今天没有异动」。
   * 锁死两条：
   *  ① 未拿到数据且加载中 ⇒ 必须显示「加载中」，**不得**出现「共 0 只」；
   *  ② 请求根本没回来（data=null）⇒ 空态文案必须指向「重试」，不得说「暂无竞价异动数据」。
   */
  it('首屏未返回时不得谎报「共 0 只」，空态文案要指向重试', async () => {
    // 永不 resolve：模拟冷查询挂住（实测 31s）
    mocks.fetchAPI.mockImplementation(() => new Promise(() => {}))
    render(<AuctionAnomalyTab market="CN" />)
    expect(await screen.findByText(/加载中/)).toBeTruthy()
    expect(screen.queryByText(/共\s*0\s*只/)).toBeNull()
  })

  it('请求失败且无数据时给出重试指引，而不是「暂无竞价异动数据」', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('请求超时'))
    render(<AuctionAnomalyTab market="CN" />)
    expect(await screen.findByText(/请求超时/)).toBeTruthy()
    expect(
      screen.getByText(/未取到竞价数据（请求未返回或超时），请点右上「刷新」重试/),
    ).toBeTruthy()
    expect(screen.queryByText(/暂无竞价异动数据/)).toBeNull()
  })
})
