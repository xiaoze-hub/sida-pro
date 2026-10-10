// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 聚宝盆选股 UI 契约(2026-10-10, 规格 §4.2)。
 * mock 网络层(fetchAPI), 组件走真实代码; 钉死: 逐条件通过/未过/降级明细 + 入池态 + 问财降级横幅。
 */
const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))

import JbpPoolTable from '@panwatch/biz-ui/components/JbpPoolTable'

function cond(key: string, status: string, detail: string) {
  return {
    key,
    name: key,
    status,
    met: status === 'pass' ? true : status === 'fail' ? false : null,
    evidence: {},
    detail,
  }
}

const RESP = {
  universe: 2,
  scanned: 2,
  in_pool: 1,
  wencai: { provided: false, available: null, hits: 0, note: '' },
  filters: {},
  note: null,
  rows: [
    {
      symbol: '000001',
      in_pool: true,
      dark_net: 5_000_000,
      activity: 13.2,
      activity_level: '更佳',
      better: true,
      gs_zone: 'G区',
      gs_signal: 'G',
      conditions: [
        cond('dark_inflow', 'pass', '暗盘净额 +5000000 元(流入)'),
        cond('activity', 'pass', '活跃度 13.20 > 6(更佳, 站上更佳线 12)'),
        cond('gs', 'pass', 'GS 区=G区, 信号=G'),
        cond('wencai', 'na', '未传关键词, 本条件不参与筛选'),
      ],
    },
    {
      symbol: '000002',
      in_pool: false,
      dark_net: -1_000_000,
      activity: 5.9,
      activity_level: '生命',
      better: false,
      gs_zone: 'G区',
      gs_signal: 'G',
      conditions: [
        cond('dark_inflow', 'fail', '暗盘净额 -1000000 元(未流入)'),
        cond('activity', 'fail', '活跃度 5.90 <= 6(未过大牛线)'),
        cond('gs', 'pass', 'GS 区=G区, 信号=G'),
        cond('wencai', 'na', '未传关键词, 本条件不参与筛选'),
      ],
    },
  ],
}

describe('JbpPoolTable 聚宝盆选股', () => {
  afterEach(() => cleanup())
  beforeEach(() => mocks.fetchAPI.mockReset())

  it('点扫描 → 逐条件通过/未过明细 + 入池态', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<JbpPoolTable embedded />)
    fireEvent.click(screen.getByText('聚宝盆扫描'))

    await waitFor(() => expect(screen.getByTestId('jbp-summary')).toBeTruthy())
    expect(screen.getByTestId('jbp-summary').textContent).toContain('入池')
    expect(screen.getByTestId('jbp-summary').textContent).toContain('1')

    // A 入池: 暗盘/活跃度通过 + 更佳标记 + 入选
    expect(screen.getByTestId('jbp-cond-000001-dark_inflow').textContent).toContain('通过')
    expect(screen.getByTestId('jbp-cond-000001-activity').textContent).toContain('通过')
    expect(screen.getByTestId('jbp-inpool-000001').textContent).toBe('入选')
    expect(screen.getByTestId('jbp-level-000001').textContent).toContain('更佳')

    // B 未入池: 暗盘未流入(未过) + 活跃度未过
    expect(screen.getByTestId('jbp-cond-000002-dark_inflow').textContent).toContain('未过')
    expect(screen.getByTestId('jbp-cond-000002-activity').textContent).toContain('未过')
    expect(screen.getByTestId('jbp-inpool-000002').textContent).toBe('未入选')
  })

  it('缺数据条件显式降级 + 该股未入选', async () => {
    const resp = {
      ...RESP,
      in_pool: 0,
      rows: [{
        ...RESP.rows[0],
        symbol: '000003',
        in_pool: false,
        better: false,
        dark_net: null,
        conditions: [
          cond('dark_inflow', 'degraded', '暗盘资金无数据(取数失败/未接入), 已降级'),
          cond('activity', 'pass', '活跃度 8.00 > 6'),
          cond('gs', 'pass', 'GS 区=G区, 信号=G'),
          cond('wencai', 'na', '未传关键词'),
        ],
      }],
    }
    mocks.fetchAPI.mockResolvedValue(resp)
    render(<JbpPoolTable embedded />)
    fireEvent.click(screen.getByText('聚宝盆扫描'))
    await waitFor(() => expect(screen.getByTestId('jbp-cond-000003-dark_inflow')).toBeTruthy())
    expect(screen.getByTestId('jbp-cond-000003-dark_inflow').textContent).toContain('降级')
    expect(screen.getByTestId('jbp-inpool-000003').textContent).toBe('未入选')
  })

  it('问财传了关键词但源不可用 → 显式降级横幅', async () => {
    mocks.fetchAPI.mockResolvedValue({
      ...RESP,
      in_pool: 0,
      rows: [],
      wencai: { provided: true, available: false, hits: 0, note: 'x' },
      note: '问财数据源不可用: 问财条件显式降级, 全池不出(不静默放宽)',
    })
    render(<JbpPoolTable embedded />)
    const kw = screen.getByLabelText('聚宝盆问财关键词') as HTMLInputElement
    fireEvent.change(kw, { target: { value: '均线多头排列' } })
    fireEvent.click(screen.getByText('聚宝盆扫描'))
    await waitFor(() => expect(screen.getByText(/问财数据源不可用/)).toBeTruthy())
  })
})
