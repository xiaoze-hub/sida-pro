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

vi.mock('@sida/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))
vi.mock('@sida/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, on: mocks.on, off: mocks.off, resize: vi.fn() } },
  }),
}))

import BoardHeatmap from '@sida/biz-ui/components/dashboard/BoardHeatmap'

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
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled(), { timeout: 5000 })

    const opt = mocks.setOption.mock.calls.at(-1)?.[0] as { series: Array<{ type: string }> }
    expect(opt.series[0].type).toBe('treemap')
    const names = lastTreemapData().map((d) => d.name)
    expect(names).toContain('半导体')
    expect(names).toContain('船舶')
    expect(screen.getByText(/数据截至 2026-09-09/)).toBeTruthy()
    expect(screen.getByText(/1 个板块无行情数据/)).toBeTruthy()
  })

  it('点击板块下钻回调 blockCode', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const onOpenBoard = vi.fn()
    render(<BoardHeatmap onOpenBoard={onOpenBoard} />)
    await waitFor(() => expect(mocks.on).toHaveBeenCalled(), { timeout: 5000 })

    const clickCall = mocks.on.mock.calls.find((c) => c[0] === 'click')
    expect(clickCall).toBeTruthy()
    const handler = clickCall![1] as (p: { data: { blockCode: string; name: string } }) => void
    handler({ data: { blockCode: 'BK0475', name: '半导体' } })
    expect(onOpenBoard).toHaveBeenCalledWith('BK0475', '半导体')
  })

  it('切换到概念板块重新请求 type=concept', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const { getByText } = render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await waitFor(() => expect(mocks.fetchAPI).toHaveBeenCalled(), { timeout: 5000 })

    fireEvent.click(getByText('概念'))
    await waitFor(() => {
      const last = String(mocks.fetchAPI.mock.calls.at(-1)?.[0] ?? '')
      expect(last).toContain('type=concept')
    })
  })

  it('刷新失败时保留上次数据并显示 amber 提示', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    const { getByTitle, findByText } = render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled(), { timeout: 5000 })

    mocks.fetchAPI.mockRejectedValueOnce(new Error('502 Bad Gateway'))
    fireEvent.click(getByTitle('刷新'))
    expect(await findByText(/刷新失败，当前展示上次成功数据/)).toBeTruthy()
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
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled(), { timeout: 5000 })
    const cells = lastTreemapData()
    expect(cells).toHaveLength(1)
    expect(cells[0].value).toBeGreaterThan(0)
  })

  it('量能视图: 有可用成交额时照常出图(空态不误伤)', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled(), { timeout: 5000 })
    expect(screen.getByTestId('heatmap-canvas')).toBeTruthy()
    expect(screen.queryByTestId('heatmap-no-volume')).toBeNull()
  })

  it('实时模式: 实时标注 + 异动清单 + 异动块警示环 + 点击下钻', async () => {
    mocks.fetchAPI.mockResolvedValue(LIVE_RESP)
    const onOpenBoard = vi.fn()
    render(<BoardHeatmap onOpenBoard={onOpenBoard} />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled(), { timeout: 5000 })

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
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled(), { timeout: 5000 })
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

  // 2026-11-09: 概念板块 ~457 条, 用户无法在色块里找到某个题材(如商业航天)。
  // 最小交互 = 本地过滤已返回数据 + 结果行明示代码/真实涨幅/来源, 点击下钻原板块。
  const CONCEPT_RESP = {
    type: 'concept',
    trade_date: '2026-11-07',
    count: 3,
    source: 'tdx',
    items: [
      { block_code: '880548', name: '商业航天', board_type: 'concept', change_pct: 2.13, fund_net: 5.2e8, volume: 3.1e10, date: '2026-11-07', has_daily: true },
      { block_code: '880001', name: '低空经济', board_type: 'concept', change_pct: -0.42, fund_net: -1e8, volume: 1.2e10, date: '2026-11-07', has_daily: true },
      { block_code: '880002', name: '空天信息', board_type: 'concept', change_pct: null, fund_net: null, volume: null, date: null, has_daily: false },
    ],
  }

  async function openConceptSearch(query: string) {
    mocks.fetchAPI.mockImplementation((path: string) =>
      Promise.resolve(String(path).includes('type=concept') ? CONCEPT_RESP : RESP),
    )
    const onOpenBoard = vi.fn()
    render(<BoardHeatmap onOpenBoard={onOpenBoard} />)
    fireEvent.click(screen.getByText('概念'))
    await waitFor(() => {
      expect(String(mocks.fetchAPI.mock.calls.at(-1)?.[0] ?? '')).toContain('type=concept')
    })
    // 等概念数据真正落到组件里(结果行依赖 items, 不依赖色块是否画得出来)
    const input = screen.getByLabelText('搜索板块') as HTMLInputElement
    fireEvent.change(input, { target: { value: query } })
    return { onOpenBoard, input }
  }

  it('概念视图可搜索到商业航天, 明示代码/真实涨幅, 点击打开原板块', async () => {
    const { onOpenBoard } = await openConceptSearch('商业航天')
    const panel = await screen.findByTestId('heatmap-search-results')
    const hit = await screen.findByTestId('heatmap-search-hit-880548')
    expect(panel.textContent).toContain('商业航天')
    expect(hit.textContent).toContain('880548')
    expect(hit.textContent).toContain('+2.13%')
    // 数据源是**响应级**事实, 只在顶部标一次; 逐项贴「通达信」会暗示每条都单独核实过来源。
    expect(hit.textContent).not.toContain('通达信')
    expect(screen.getAllByText('通达信')).toHaveLength(1)
    fireEvent.click(hit)
    expect(onOpenBoard).toHaveBeenCalledWith('880548', '商业航天')
  })

  it('搜索只过滤后端已返回板块; 无匹配时明说未找到, 不编造条目', async () => {
    await openConceptSearch('不存在的题材')
    expect(await screen.findByTestId('heatmap-search-results')).toBeTruthy()
    expect(screen.getByText(/未找到匹配板块/)).toBeTruthy()
    expect(screen.queryByTestId('heatmap-search-hit-880548')).toBeNull()
  })

  it('搜索框为空时不展示结果面板(不倾倒全量板块)', async () => {
    await openConceptSearch('')
    expect(screen.queryByTestId('heatmap-search-results')).toBeNull()
  })

  it('搜索命中无当日数据的板块时如实标注, 不显示伪造涨幅/实时', async () => {
    await openConceptSearch('空天信息')
    const hit = await screen.findByTestId('heatmap-search-hit-880002')
    expect(hit.textContent).toContain('无数据')
    expect(hit.textContent).not.toContain('实时')
  })

  // ── 状态诚实性(2026-11-09 复审): 加载中 / 失败 / 空载荷 / 总数 / 旧数据 不能互相冒充 ──
  const MANY_CONCEPT = {
    type: 'concept',
    trade_date: '2026-11-07',
    count: 15,
    source: 'tdx',
    items: Array.from({ length: 15 }, (_, i) => ({
      block_code: `88${1000 + i}`,
      name: `航天测试板块${i}`,
      board_type: 'concept',
      change_pct: 1 + i / 100,
      fund_net: null,
      volume: 1e9,
      date: '2026-11-07',
      has_daily: true,
    })),
  }

  it('搜索结果标明真实匹配总数, 不把 12 条展示上限当总数', async () => {
    mocks.fetchAPI.mockImplementation((path: string) =>
      Promise.resolve(String(path).includes('type=concept') ? MANY_CONCEPT : RESP),
    )
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    fireEvent.click(screen.getByText('概念'))
    await waitFor(() => {
      expect(String(mocks.fetchAPI.mock.calls.at(-1)?.[0] ?? '')).toContain('type=concept')
    })
    fireEvent.change(screen.getByLabelText('搜索板块'), { target: { value: '航天测试板块' } })

    const panel = await screen.findByTestId('heatmap-search-results')
    await waitFor(() => expect(panel.querySelectorAll('[data-testid^="heatmap-search-hit-"]').length).toBe(12))
    expect(panel.textContent).toContain('共 15 个匹配')
    expect(panel.textContent).toContain('仅显示前 12 个')
  })

  it('当前分类是空载荷时明说无数据, 不说「未找到匹配」(那是查询没命中的说法)', async () => {
    mocks.fetchAPI.mockResolvedValue({ type: 'industry', trade_date: null, count: 0, items: [] })
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await screen.findByText('暂无板块数据（等待每日同步）')
    fireEvent.change(screen.getByLabelText('搜索板块'), { target: { value: '半导体' } })

    const panel = await screen.findByTestId('heatmap-search-results')
    expect(panel.textContent).toContain('暂无板块数据')
    expect(panel.textContent).not.toContain('未找到匹配板块')
  })

  it('数据请求失败时搜索面板如实说失败, 不写「加载中」', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('502 Bad Gateway'))
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await screen.findByText('数据加载失败')

    fireEvent.change(screen.getByLabelText('搜索板块'), { target: { value: '半导体' } })
    const panel = await screen.findByTestId('heatmap-search-results')
    expect(panel.textContent).not.toContain('加载中')
    expect(panel.textContent).toContain('失败')
  })

  it('切换分类时旧分类的失败不得冒充新分类状态: 先加载, 失败后明确失败', async () => {
    let rejectConcept: ((e: unknown) => void) | null = null
    mocks.fetchAPI.mockImplementation((path: string) => {
      if (String(path).includes('type=concept')) {
        return new Promise((_res, rej) => {
          rejectConcept = rej
        })
      }
      return Promise.reject(new Error('502 Bad Gateway'))
    })
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await screen.findByText('数据加载失败')

    fireEvent.click(screen.getByText('概念'))
    // 新分类尚无结果: 不许把旧分类的错误当成新分类的
    await waitFor(() => expect(screen.queryByText('数据加载失败')).toBeNull())
    fireEvent.change(screen.getByLabelText('搜索板块'), { target: { value: '商业航天' } })
    const panel = await screen.findByTestId('heatmap-search-results')
    expect(panel.textContent).toContain('加载中')

    await waitFor(() => expect(rejectConcept).not.toBeNull())
    rejectConcept!(new Error('concept 502'))
    await waitFor(() => expect(panel.textContent).not.toContain('加载中'))
    expect(screen.getByText('数据加载失败')).toBeTruthy()
    expect(panel.textContent).toContain('失败')
  })

  it('刷新失败标注旧数据及其日期, 而不是笼统一句「上次数据」', async () => {
    mocks.fetchAPI.mockResolvedValue(RESP)
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    await waitFor(() => expect(mocks.setOption).toHaveBeenCalled(), { timeout: 5000 })

    mocks.fetchAPI.mockRejectedValueOnce(new Error('502 Bad Gateway'))
    fireEvent.click(screen.getByTitle('刷新'))
    const banner = await screen.findByText(/刷新失败/)
    expect(banner.textContent).toContain('上次成功数据')
    expect(banner.textContent).toContain('2026-09-09')
  })

  it('has_daily=false 但有实时覆盖: 标「仅实时」, 不说「暂无当日数据/无当日行情」', async () => {
    mocks.fetchAPI.mockResolvedValue({
      type: 'concept',
      trade_date: '2026-11-07',
      count: 2,
      live: true,
      live_count: 1,
      as_of: '2026-11-07T06:05:00+00:00',
      source: 'ths',
      items: [
        { block_code: '880900', name: '实时覆盖板块', board_type: 'concept', change_pct: 1.5, fund_net: null, volume: 1e9, date: null, has_daily: false, live: true, volume_ratio: 1.2, speed: 0.3 },
        { block_code: '880901', name: '空数据板块', board_type: 'concept', change_pct: null, fund_net: null, volume: null, date: null, has_daily: false },
      ],
    })
    render(<BoardHeatmap onOpenBoard={vi.fn()} />)
    fireEvent.click(screen.getByText('概念'))
    await waitFor(() => {
      expect(String(mocks.fetchAPI.mock.calls.at(-1)?.[0] ?? '')).toContain('type=concept')
    })
    fireEvent.change(screen.getByLabelText('搜索板块'), { target: { value: '实时覆盖板块' } })
    const hit = await screen.findByTestId('heatmap-search-hit-880900')
    expect(hit.textContent).toContain('仅实时')
    expect(hit.textContent).toContain('+1.50%')
    expect(screen.queryByText(/暂无当日数据/)).toBeNull()
    expect(screen.queryByText(/无当日行情/)).toBeNull()
  })

  it('搜索结果不逐项重复响应级数据源(来源只在响应层级标一次)', async () => {
    await openConceptSearch('航天')
    const panel = await screen.findByTestId('heatmap-search-results')
    await screen.findByTestId('heatmap-search-hit-880548')
    expect(panel.textContent).not.toContain('通达信')
    expect(screen.getAllByText('通达信')).toHaveLength(1)
  })
})
