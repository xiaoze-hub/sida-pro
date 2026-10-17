// @vitest-environment jsdom
/**
 * I-4 转债-正股联动套利雷达(2026-10-18)
 *
 * 守四件事:
 * ① **数据日/交易阶段来自后端**(A-03 同一铁律): 非实时时必须显示"数据日 + 阶段", 不许出现
 *    浏览器时钟式的"实时";
 * ② 指标按后端原值渲染: 溢价率 -1.50%(折价)、双低值带全池排名、强赎距离 +2.52%;
 * ③ 场景徽章来自后端 `scenarios`(折价套利 / 强赎触发区), 前端不自己重新判定;
 * ④ 数据源不可用 ⇒ 如实显示 note, **不出现任何编造的转债行**。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: mocks.fetchAPI,
  getToken: () => '',
}))

import CbRadarPanel from '../../packages/biz-ui/src/components/CbRadarPanel'

const ROW = {
  code: '113550',
  name: '转债一号',
  stock_code: '600111',
  stock_name: '北方稀土',
  rating: 'AA',
  bond_price: 118.5,
  bond_chg_pct: 1.2,
  conv_price: 10,
  conv_value: 120.3,
  conv_chg_pct: 3.1,
  premium_pct: -1.5,
  double_low: 117.0,
  double_low_rank: 3,
  stock_price: 12.03,
  stock_chg_pct: 3.1,
  lag_pct: 1.9,
  force_redeem_price: 11.73,
  redeem_distance_pct: 2.56,
  pure_bond_value: 95.0,
  pure_bond_premium_pct: 24.74,
  end_date: '2029-06-30',
  scenarios: [
    { key: 'force_redeem_breached', label: '强赎触发区', tone: 'risk', detail: '正股 12.03 ≥ 强赎触发价 11.73（+2.56%）' },
    { key: 'discount_arb', label: '折价套利', tone: 'opportunity', detail: '溢价率 -1.50%' },
  ],
  scenario_keys: ['force_redeem_breached', 'discount_arb'],
}

const SNAPSHOT = {
  available: true,
  data_date: '2026-10-16',
  trade_phase: 'post_close',
  phase_label: '收盘后',
  is_live: false,
  is_today: true,
  as_of: '2026-10-18T15:03:12+08:00',
  source: '通达信 TQ',
  caliber: '转股价/强赎触发价/纯债价值均为客户端原值，未自行贴现或估算',
  universe: 512,
  evaluated: 1,
  rows: [ROW],
  scenario_counts: { force_redeem_breached: 1, discount_arb: 1 },
  cached: false,
  age_s: 0,
}

afterEach(() => {
  cleanup()
  mocks.fetchAPI.mockReset()
})

describe('CbRadarPanel', () => {
  it('非实时时显示"数据日 + 交易阶段", 并渲染后端原值与场景', async () => {
    mocks.fetchAPI.mockResolvedValue(SNAPSHOT)
    render(<CbRadarPanel limit={60} />)

    expect(await screen.findByText('转债一号')).toBeTruthy()
    // ① 数据日/阶段来自后端, 不是浏览器时钟
    expect(screen.getByText('2026-10-16')).toBeTruthy()
    expect(screen.getByText(/收盘后\s*·\s*今日已定型/)).toBeTruthy()
    // ② 原值渲染
    expect(screen.getByText('-1.50%')).toBeTruthy()
    expect(screen.getByText('#3')).toBeTruthy()
    expect(screen.getByText('+2.56%')).toBeTruthy()
    // ③ 场景徽章来自后端
    expect(screen.getAllByText('折价套利').length).toBeGreaterThan(0)
    expect(screen.getAllByText('强赎触发区').length).toBeGreaterThan(0)
    // 口径与免责
    expect(screen.getByText(/不构成投资建议/)).toBeTruthy()
    // 只打一个端点(不额外扇出)
    expect(mocks.fetchAPI).toHaveBeenCalledWith('/cb-radar/snapshot?limit=60', expect.anything())
  })

  it('数据源不可用时如实显示 note, 不渲染任何行', async () => {
    mocks.fetchAPI.mockResolvedValue({
      available: false,
      rows: [],
      note: '转账条款不可用（TQ get_kzz_info 全部失败）',
      scenario_counts: {},
    })
    render(<CbRadarPanel />)
    expect(await screen.findByText(/条款不可用/)).toBeTruthy()
    expect(screen.queryByText('转债一号')).toBeNull()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('实时窗口内显示"实时"而不是数据日徽章', async () => {
    mocks.fetchAPI.mockResolvedValue({
      ...SNAPSHOT,
      is_live: true,
      trade_phase: 'auction',
      phase_label: '集合竞价 09:15-09:25',
      as_of: '2026-10-16T09:20:05+08:00',
      data_date: '2026-10-16',
    })
    render(<CbRadarPanel />)
    expect(await screen.findByText('实时')).toBeTruthy()
    expect(screen.getByText('09:20:05')).toBeTruthy()
  })
})
