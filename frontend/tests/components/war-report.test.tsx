// @vitest-environment jsdom
// 主力资金战报页(规格 §4.4, 2026-10-10):
//   ① 有快照 → 渲染大单净流入 TOP/BOTTOM 行(万元口径带符号) + 净额变化;
//   ② 缺源子块(行业 / 拆单对倒)显式「无数据」, 不塌成 0;
//   ③ 无快照 → 显式提示 + 生成按钮, 不出空表。
// 禁真实网络: mock @panwatch/api, 组件走真实代码。
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ daily: vi.fn(), refresh: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  warReportApi: {
    daily: (...a: unknown[]) => mocks.daily(...a),
    refresh: (...a: unknown[]) => mocks.refresh(...a),
  },
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))

import WarReportPage from '@/pages/WarReport'

function row(over: Record<string, unknown> = {}) {
  return {
    symbol: '600519',
    name: '贵州茅台',
    main_net_wan: 88000,
    net_change_wan: 80,
    samples: 12,
    last_sample_ts: '10:00',
    source: 'thsdk_dde',
    caliber: 'ths',
    ...over,
  }
}

function snapshot() {
  return {
    available: true,
    snapshot_date: '2026-10-09',
    market: 'CN',
    updated_at: '2026-10-09T15:31:02',
    generated_at: '2026-10-09T15:31:02',
    universe: 5141,
    computed: 5100,
    top_inflow: [row()],
    top_outflow: [row({ symbol: '000001', name: '平安银行', main_net_wan: -50000, net_change_wan: null })],
    industry: { available: false, note: '行业资金(SUPAMO)源不可用', rows: [] },
    split_wash: {
      available: false, split_count: null, wash_count: null, covered: [],
      note: '无委托号级 .tck 数据源',
    },
  }
}

describe('WarReport 主力资金战报', () => {
  afterEach(() => cleanup())
  beforeEach(() => mocks.daily.mockReset())

  it('有快照 → 渲染 TOP/BOTTOM 行 + 万元口径(带符号) + 净额变化', async () => {
    mocks.daily.mockResolvedValue(snapshot())
    render(<WarReportPage />)
    await screen.findAllByText('贵州茅台')

    expect(screen.getByText('+8.80亿')).toBeTruthy() // 88000 万 → 8.80 亿
    expect(screen.getByText('+80.00万')).toBeTruthy() // 净额变化
    expect(screen.getByTestId('war-report')).toBeTruthy()
  })

  it('缺源子块: 行业 / 拆单对倒显式「无数据」而非 0', async () => {
    mocks.daily.mockResolvedValue(snapshot())
    render(<WarReportPage />)
    await screen.findAllByText('贵州茅台')

    expect(screen.getByText(/行业资金\(SUPAMO\)暂无数据/)).toBeTruthy()
    expect(screen.getByText(/拆单\/对倒暂无数据源/)).toBeTruthy()
  })

  it('无快照 → 显式提示 + 生成按钮, 不渲染表格', async () => {
    mocks.daily.mockResolvedValue({ available: false, note: '本次无快照说明(测试)' })
    render(<WarReportPage />)
    await screen.findByText('本次无快照说明(测试)')

    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.getByText(/立即生成/)).toBeTruthy()
  })
})
