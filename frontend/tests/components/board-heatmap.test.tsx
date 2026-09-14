// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// P1-1 板块热力图组件 (2026-09-10): treemap 渲染 + 点击下钻 + 空/错/stale 三态。
const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  setOption: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('@panwatch/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, on: mocks.on, off: mocks.off, resize: vi.fn() } },
  }),
}))

import BoardHeatmap from '@panwatch/biz-ui/components/dashboard/BoardHeatmap'

const RESP = {
  type: 'industry',
  trade_date: '2026-09-09',
  count: 3,
  items: [
    { block_code: 'BK0475', name: '半导体', board_type: 'industry', change_pct: 2.35, fund_net: 1.2e9, volume: 8.8e10, date: '2026-09-09', has_daily: true },
    { block_code: 'BK0477', name: '白酒', board_type: 'industry', change_pct: -1.2, fund_net: -3e8, volume: 2.1e10, date: '2026-09-09', has_daily: true },
    { block_code: 'BK9999', name: '船舶', board_type: 'industry', change_pct: null, fund_net: null, volume: null, date: null, has_daily: false },
  ],
}

type CellLike = {
  name: string
  value: number
  blockCode: string
  anomaly: { label: string } | null
  itemStyle?: { borderWidth?: number }
}
/** 最近一次 setOption 的 treemap 数据(全字段) */
function lastTreemapData(): CellLike[] {
  const opt = mocks.setOption.mock.calls.at(-1)?.[0] as {
    series?: Array<{ type: string; data?: CellLike[] }>
  }
  return opt?.series?.[0]?.data ?? []
}

/** 实时模式响应(2026-09-10 盘中实时化): live=true + 量比/涨速字段 */
const LIVE_RESP = {
  type: 'industry',
  trade_date: '2026-09-10',
  count: 2,
  live: true,
  live_count: 2,
  as_of: '2026-09-10T06:05:00+00:00',
  items: [
    { block_code: 'BK0475', name: '半导体', board_type: 'industry', change_pct: 2.35, fund_net: 1.2e9, volume: 8.8e10, date: '2026-09-10', has_daily: true, volume_ratio: 2.4, speed: 0.9, live: true },
    { block_code: 'BK0477', name: '白酒', board_type: 'industry', change_pct: -0.2, fund_net: -1e8, volume: 2.1e10, date: '2026-09-10', has_daily: true, volume_ratio: 1.1, speed: 0.1, live: true },
  ],
}

describe('BoardHeatmap 板块热力图', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    mocks.fetchAPI.mockReset()
    mocks.setOption.mockReset()
    mocks.on.mockReset()
    mocks.off.mockReset()
  })

  it('渲染 treemap series 并标注数据截至日期', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())

    const opt = mocks.setOption.mock.calls.at(-1)?.[0] as { series: Array<{ type: string }> }
    expect(opt.series[0].type).toBe('treemap')
    const names = lastTreemapData().map((d) => d.name)
    expect(names).toContain('半导体')
    expect(names).toContain('船舶')
    expect(screen.getByText(/数据截至 2026-09-09/)).toBeTruthy()
    expect(screen.getByText(/1 个暂无当日数据/)).toBeTruthy()
  })

  it('点击板块下钻回调 blockCode', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const onOpenBoard = vi.fn()
    render(<BoardHeatmap onOpenBoard={onOpenBoard} />)
    await waitFor(() => expect(mocks.on).toHaveBeenCalled())

    const clickCall = mocks.on.mock.calls.find((c) => c[0] === 'click')
    expect(clickCall).toBeTruthy()
    const handler = clickCall![1] as (p: { data: { blockCode: string; name: string } }) => void
    handler({ data: { blockCode: 'BK0475', name: '半导体' } })
    expect(onOpenBoard).toHaveBeenCalledWith('BK0475', '半导体')
  })

  it('切换到概念板块重新请求 type=concept', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const { getByText } = render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await waitFor(() => expect(mocks.fetchAPI).toHaveBeenCalled())

    fireEvent.click(getByText('概念'))
    await waitFor(() => {
      const last = String(mocks.fetchAPI.mock.calls.at(-1)?.[0] ?? '')
      expect(last).toContain('type=concept')
    })
  })

  it('刷新失败时保留上次数据并显示 amber 提示', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const { getByTitle, findByText } = render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())

    mocks.fetchAPI.mockRejectedValueOnce(new Error('502 Bad Gateway'))
    fireEvent.click(getByTitle('刷新'))
    expect(await findByText(/刷新失败，当前展示上次数据/)).toBeTruthy()
    // 数据未被清空: treemap 仍在
    expect(lastTreemapData().length).toBe(3)
  })

  it('无数据时展示空态文案', async () => {
    mocks.fetchAPI.mockResolvedValue({ type: 'industry', trade_date: null, count: 0, items: [] })
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    expect(await screen.findByText('暂无板块数据（等待每日同步）')).toBeTruthy()
    // 空列表不许画一张空图(静默空白画布是缺陷本体)
    expect(mocks.setOption).not.toHaveBeenCalled()
  })

  // 2026-09-14 缺陷修复(生产截图: 默认「面积:量能」整块空白且无任何说明):
  // 后端整批 volume=0 / change_pct=-100 的载荷下, 量能视图必须显式说明而不是留白。
  it('量能视图: 全部板块成交额缺失/为 0 → 显式空态 + 不给画布(不是静默空白)', async () => {
    mocks.fetchAPI.mockResolvedValue({
      type: 'industry',
      trade_date: '2026-09-11',
      count: 2,
      items: [
        { block_code: 'BK1', name: '半导体', board_type: 'industry', change_pct: -100, fund_net: null, volume: 0, date: '2026-09-11', has_daily: false },
        { block_code: 'BK2', name: '白酒', board_type: 'industry', change_pct: -100, fund_net: null, volume: null, date: '2026-09-11', has_daily: false },
      ],
    })
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    const empty = await screen.findByTestId('heatmap-no-volume')
    expect(empty.textContent).toContain('成交额全部缺失')
    expect(empty.textContent).toContain('已停绘')
    // 没有画布(没有"静默空白灰框"), 也没有对全 0 面积调 setOption
    expect(screen.queryByTestId('heatmap-canvas')).toBeNull()
    expect(mocks.setOption).not.toHaveBeenCalled()
  })

  it('量能视图空态可以一键切到「面积:等权」并恢复出图(信息不丢)', async () => {
    mocks.fetchAPI.mockResolvedValue({
      type: 'industry',
      trade_date: '2026-09-11',
      count: 1,
      items: [
        { block_code: 'BK1', name: '半导体', board_type: 'industry', change_pct: 1.5, fund_net: null, volume: 0, date: '2026-09-11', has_daily: false },
      ],
    })
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await screen.findByTestId('heatmap-no-volume')

    fireEvent.click(screen.getByText('切换面积:等权'))
    expect(await screen.findByTestId('heatmap-canvas')).toBeTruthy()
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    const cells = lastTreemapData()
    expect(cells).toHaveLength(1)
    expect(cells[0].value).toBeGreaterThan(0)
  })

  it('量能视图: 有可用成交额时照常出图(空态不误伤)', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    expect(screen.getByTestId('heatmap-canvas')).toBeTruthy()
    expect(screen.queryByTestId('heatmap-no-volume')).toBeNull()
  })

  it('实时模式: 实时标注 + 异动清单 + 异动块警示环 + 点击下钻', async () => {
    mocks.fetchAPI.mockResolvedValue(LIVE_RESP)
    const onOpenBoard = vi.fn()
    render(<BoardHeatmap onOpenBoard={onOpenBoard} />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())

    expect(screen.getByText(/实时/)).toBeTruthy()
    expect(screen.queryByText(/数据截至/)).toBeNull()

    const strip = screen.getByTestId('heatmap-anomalies')
    expect(strip.textContent).toContain('半导体')
    expect(strip.textContent).toContain('急拉 · 放量')
    expect(strip.textContent).not.toContain('白酒')

    fireEvent.click(screen.getByTitle('半导体 急拉 · 放量'))
    expect(onOpenBoard).toHaveBeenCalledWith('BK0475', '半导体')

    const cells = lastTreemapData()
    expect(cells.find((c) => c.blockCode === 'BK0475')?.itemStyle?.borderWidth).toBe(2)
    expect(cells.find((c) => c.blockCode === 'BK0477')?.itemStyle?.borderWidth ?? 0).toBe(0)
    expect(cells.find((c) => c.blockCode === 'BK0475')?.anomaly?.label).toBe('急拉 · 放量')
  })

  it('非实时(仅日线)不出现异动清单与实时标注', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled())
    expect(screen.queryByTestId('heatmap-anomalies')).toBeNull()
    expect(screen.queryByText(/实时/)).toBeNull()
    expect(screen.getByText(/数据截至 2026-09-09/)).toBeTruthy()
  })

  it('首次加载失败时展示错误态并可重试', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('boom'))
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    expect(await screen.findByText('数据加载失败')).toBeTruthy()
    expect(screen.getByText('重试')).toBeTruthy()
  })
})
