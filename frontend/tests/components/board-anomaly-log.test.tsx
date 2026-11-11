// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 板块异动记录面板 (2026-11-11): 图下方的**带时间**历史记录。
 * 与顶部瞬时芯片(heatmap-anomalies)并存, 但数据只认 /boards/anomalies 的返回 ——
 * 本用例守四件事: ①真实字段渲染 + 覆盖行; ②行可下钻; ③空态不编造; ④失败不连坐热力图。
 */
const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  setOption: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
}))

vi.mock('@sida/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('@sida/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, on: mocks.on, off: mocks.off, resize: vi.fn() } },
  }),
}))

import BoardAnomalyLog from '@sida/biz-ui/components/dashboard/BoardAnomalyLog'
import BoardHeatmap from '@sida/biz-ui/components/dashboard/BoardHeatmap'

/** 本地今日(面板只在 trade_date 等于今天时说「今日」; 用当天日期避免依赖机器时区假绿) */
function todayLocal(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

const RESP = {
  available: true,
  trade_date: todayLocal(),
  source: 'tdx_tq',
  coverage: {
    samples: 118,
    first_ts: '2026-11-11T09:31:04+08:00',
    last_ts: '2026-11-11T15:00:12+08:00',
    boards: 486,
    interval_s: 120,
  },
  count: 2,
  items: [
    {
      ts: '2026-11-11T10:32:00+08:00',
      block_code: '881290.SH',
      name: '半导体',
      board_type: 'concept',
      kind: 'surge',
      label: '急拉',
      value: 0.62,
      window_min: 5,
      change_pct: 2.31,
      fund_net: null,
    },
    {
      ts: '2026-11-11T13:05:00+08:00',
      block_code: '881101.SH',
      name: '白酒',
      board_type: 'concept',
      kind: 'dump',
      label: '急跌',
      value: -0.71,
      window_min: 5,
      change_pct: -1.42,
      fund_net: null,
    },
  ],
  note: '记录自 09:31 首次采样起；采样中断的时段没有记录。',
}

const HEATMAP_RESP = {
  type: 'industry',
  trade_date: '2026-11-11',
  count: 2,
  items: [
    { block_code: 'BK0475', name: '半导体', board_type: 'industry', change_pct: 2.35, fund_net: 1.2e9, volume: 8.8e10, date: '2026-11-11', has_daily: true },
    { block_code: 'BK0477', name: '白酒', board_type: 'industry', change_pct: -1.2, fund_net: -3e8, volume: 2.1e10, date: '2026-11-11', has_daily: true },
  ],
}

describe('BoardAnomalyLog 板块异动记录', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    mocks.fetchAPI.mockReset()
    mocks.setOption.mockReset()
    mocks.on.mockReset()
    mocks.off.mockReset()
  })

  it('渲染时间/板块/类型/数值/当前涨幅, 并显示覆盖行', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<BoardAnomalyLog boardType="concept" onOpenBoard={vi.fn()} />)

    const rows = await screen.findAllByTestId('board-anomaly-row')
    expect(rows).toHaveLength(2)

    const surge = rows[0].textContent ?? ''
    expect(surge).toContain('10:32')
    expect(surge).toContain('半导体')
    expect(surge).toContain('急拉')
    expect(surge).toContain('涨速 +0.62%')
    expect(surge).toContain('/ 5分钟')
    expect(surge).toContain('+2.31%')

    const dump = rows[1].textContent ?? ''
    expect(dump).toContain('13:05')
    expect(dump).toContain('急跌')
    // 方向由 kind 定: 后端给 -0.71 也不会渲染成「+0.71% 的急涨」
    expect(dump).toContain('跌速 -0.71%')
    expect(dump).toContain('-1.42%')

    const coverage = screen.getByTestId('board-anomaly-coverage').textContent ?? ''
    expect(coverage).toContain('今日')
    expect(coverage).toContain('09:31–15:00')
    expect(coverage).toContain('采样 118 次')

    // 采样中断的说明原样带出(不省略, 否则读者会以为记录是连续完整的)
    expect(screen.getByTestId('board-anomaly-log').textContent).toContain('采样中断的时段没有记录')

    // 请求口径: type + limit
    const path = String(mocks.fetchAPI.mock.calls[0][0])
    expect(path).toContain('/boards/anomalies')
    expect(path).toContain('type=concept')
    expect(path).toContain('limit=50')
  })

  it('点击行回调 onOpenBoard(block_code, name)', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const onOpenBoard = vi.fn()
    render(<BoardAnomalyLog boardType="concept" onOpenBoard={onOpenBoard} />)

    const rows = await screen.findAllByTestId('board-anomaly-row')
    fireEvent.click(rows[0])
    expect(onOpenBoard).toHaveBeenCalledWith('881290.SH', '半导体')
    fireEvent.click(rows[1])
    expect(onOpenBoard).toHaveBeenCalledWith('881101.SH', '白酒')
  })

  it('items 为空: 说「暂无采样」, 不渲染任何记录行也不伪造数据', async () => {
    mocks.fetchAPI.mockResolvedValue({
      available: true,
      trade_date: todayLocal(),
      coverage: { samples: 0, first_ts: null, last_ts: null, boards: 0, interval_s: 120 },
      count: 0,
      items: [],
      note: '记录自 09:31 首次采样起；采样中断的时段没有记录。',
    })
    render(<BoardAnomalyLog boardType="industry" onOpenBoard={vi.fn()} />)

    const status = await screen.findByTestId('board-anomaly-log-status')
    expect(status.textContent).toContain('今日暂无采样（交易时段每 2 分钟采样一次）')
    expect(screen.queryAllByTestId('board-anomaly-row')).toHaveLength(0)
    // 没有采样就不许出现"覆盖行"(那会暗示今天真的有数据)
    expect(screen.queryByTestId('board-anomaly-coverage')).toBeNull()
    expect(screen.getByTestId('board-anomaly-log').textContent).not.toContain('急拉')
  })

  it('available=false: 明说「异动记录暂不可用」并带出后端说明', async () => {
    mocks.fetchAPI.mockResolvedValue({
      available: false,
      trade_date: todayLocal(),
      count: 0,
      items: [],
      note: '异动采样未启用',
    })
    render(<BoardAnomalyLog boardType="industry" onOpenBoard={vi.fn()} />)

    const status = await screen.findByTestId('board-anomaly-log-status')
    expect(status.textContent).toContain('异动记录暂不可用')
    expect(status.textContent).toContain('异动采样未启用')
    expect(screen.queryAllByTestId('board-anomaly-row')).toHaveLength(0)
  })

  it('接口抛错: 显示一行「异动记录暂不可用」', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('502 Bad Gateway'))
    render(<BoardAnomalyLog boardType="industry" onOpenBoard={vi.fn()} />)

    const status = await screen.findByTestId('board-anomaly-log-status')
    expect(status.textContent).toContain('异动记录暂不可用')
    expect(screen.queryAllByTestId('board-anomaly-row')).toHaveLength(0)
  })

  it('响应缺契约字段(无 available): 按不可用处理, 不当空列表糊过去', async () => {
    mocks.fetchAPI.mockResolvedValue({ type: 'industry', trade_date: null, count: 0, items: [] })
    render(<BoardAnomalyLog boardType="industry" onOpenBoard={vi.fn()} />)

    const status = await screen.findByTestId('board-anomaly-log-status')
    expect(status.textContent).toContain('异动记录暂不可用')
  })

  it('refreshKey 递增触发重新取数(与热力图 60s 同一条刷新链路)', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const { rerender } = render(<BoardAnomalyLog boardType="concept" onOpenBoard={vi.fn()} refreshKey={0} />)
    await screen.findAllByTestId('board-anomaly-row')
    expect(mocks.fetchAPI).toHaveBeenCalledTimes(1)

    rerender(<BoardAnomalyLog boardType="concept" onOpenBoard={vi.fn()} refreshKey={1} />)
    await waitFor(() => expect(mocks.fetchAPI).toHaveBeenCalledTimes(2))
  })

  it('超过 40 行只展示前 40 条, 并如实说「还有 N 条」', async () => {
    const items = Array.from({ length: 45 }, (_, i) => ({
      ts: `2026-11-11T10:${String(i).padStart(2, '0')}:00+08:00`,
      block_code: `8812${String(i).padStart(2, '0')}.SH`,
      name: `测试板块${i}`,
      board_type: 'concept',
      kind: 'heavy_volume' as const,
      label: '放量',
      value: 2.4,
      window_min: null,
      change_pct: 1.1,
      fund_net: null,
    }))
    mocks.fetchAPI.mockResolvedValue({
      available: true,
      trade_date: todayLocal(),
      coverage: { samples: 118, first_ts: '2026-11-11T09:31:04+08:00', last_ts: '2026-11-11T15:00:12+08:00', boards: 486, interval_s: 120 },
      count: 45,
      items,
    })
    render(<BoardAnomalyLog boardType="concept" onOpenBoard={vi.fn()} />)

    expect(await screen.findAllByTestId('board-anomaly-row')).toHaveLength(40)
    expect(screen.getByTestId('board-anomaly-more').textContent).toContain('还有 5 条')
    // 放量口径: 量比倍数, 不是涨速
    const first = screen.getAllByTestId('board-anomaly-row')[0].textContent ?? ''
    expect(first).toContain('量比 2.4x')
  })

  it('异动接口失败时热力图照常渲染(不连坐)', async () => {
    mocks.fetchAPI.mockImplementation((path: string) =>
      String(path).includes('/boards/anomalies')
        ? Promise.reject(new Error('502 Bad Gateway'))
        : Promise.resolve(HEATMAP_RESP),
    )
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)

    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled(), { timeout: 5000 })
    expect(screen.getByTestId('heatmap-canvas')).toBeTruthy()

    const status = await screen.findByTestId('board-anomaly-log-status')
    expect(status.textContent).toContain('异动记录暂不可用')
    expect(screen.queryAllByTestId('board-anomaly-row')).toHaveLength(0)
  })

  it('热力图带出的异动记录可点击下钻(与色块/芯片同一行为)', async () => {
    mocks.fetchAPI.mockImplementation((path: string) =>
      Promise.resolve(String(path).includes('/boards/anomalies') ? RESP : HEATMAP_RESP),
    )
    const onOpenBoard = vi.fn()
    render(<BoardHeatmap onOpenBoard={onOpenBoard} />)

    const rows = await screen.findAllByTestId('board-anomaly-row')
    expect(rows).toHaveLength(2)
    fireEvent.click(rows[0])
    expect(onOpenBoard).toHaveBeenCalledWith('881290.SH', '半导体')
  })
})
