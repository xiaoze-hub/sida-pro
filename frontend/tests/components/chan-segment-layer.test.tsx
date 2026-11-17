// @vitest-environment jsdom
//
// 缠论 **线段** 与 **操作口径中枢** 子图层（2026-11-18）—— 组件侧接线回归（渲染真 KlineChart）。
//
// 钉住的口径（与后端 `segments` / `operational_centre` 块一一对应）：
//   1. 两个子图层**默认关**，且与分型/笔开关互不影响；
//   2. 线段：已确认=粗实线、尾部候选=细虚线且**显式可漂移**；blocked/pending **不画**、
//      不进图例几何（只计数）；
//   3. 操作口径中枢：zd/zg 两条水平线，标签一律写「操作口径·非严格中枢」；
//      not_enough_confirmed_segments **不得**说成"无中枢"，no_centre 才是实测无交集；
//   4. 跨快照（as_of_day / 标的 / pen digest）+ 标签漂移 一律拒画，并清空自身图层；
//   5. 关图层/切股先撤自己的 series 与价位线，分型 marker、笔线、别的价位线一条不动；
//      不许出现任何方向性结论或买卖语义。
//
// 图表库被 vite.config.ts 的 test.alias 换成无副作用存根；本文件再包一层，把
// "组件到底往图里加了哪些 series / 哪些价位线、后来有没有撤掉"记下来（canvas 上没有 DOM 可查）。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

const DIGEST_A = 'a'.repeat(64)
const DIGEST_B = 'b'.repeat(64)
const SEGMENTS_DIGEST = 'c'.repeat(64)

type Bar = { date: string; open: number; high: number; low: number; close: number; volume?: number }
type Marker = Record<string, unknown> & { chanDay?: string; chanStatus?: string; text?: string }
type RecSeries = { type: string; options: Record<string, unknown>; data: unknown[]; removed: boolean }
type RecPriceLine = {
  price?: number
  title?: string
  lineStyle?: number
  color?: string
  removed: boolean
}

/** 假后端的可编程面 + 假图表库的记账面。 */
const H = vi.hoisted(() => ({
  calls: [] as string[],
  markers: [] as unknown[],
  series: [] as Array<{ type: string; options: Record<string, unknown>; data: unknown[]; removed: boolean }>,
  priceLines: [] as Array<{ price?: number; title?: string; lineStyle?: number; color?: string; removed: boolean }>,
  klines: null as null | ((url: string) => Promise<unknown>),
  structure: null as null | ((url: string) => Promise<unknown>),
}))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string, options?: Record<string, unknown>) => {
    const u = String(url)
    H.calls.push(u)
    void options
    if (u.includes('/chan-structure')) {
      return H.structure ? H.structure(u) : Promise.resolve({})
    }
    if (u.includes('/klines/') && u.includes('?')) {
      return H.klines ? H.klines(u) : Promise.resolve({ klines: [] })
    }
    // summary / patterns / wyckoff / chart-notes / scenario / indicators … 一律空响应
    return Promise.resolve({})
  }),
  fetchScenario: vi.fn(() => Promise.resolve(null)),
}))

vi.mock('lightweight-charts', async () => {
  const stub = await import('../../tests/stubs/lightweight-charts')
  const base = stub as unknown as {
    createChart: () => Record<string, unknown>
    LineStyle: Record<string, number>
  }
  return {
    ...base,
    LineStyle: { ...base.LineStyle, Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3 },
    createChart: () => {
      const chart = base.createChart()
      const ownedSeries: RecSeries[] = []
      const ownedLines: RecPriceLine[] = []
      const addSeries = chart.addSeries as (type: unknown) => Record<string, unknown>
      chart.addSeries = (type: unknown, seriesOptions?: unknown) => {
        const series = addSeries(type)
        const rec: RecSeries = {
          type: String((type as { type?: string } | null)?.type ?? type),
          options: { ...((seriesOptions as Record<string, unknown>) ?? {}) },
          data: [],
          removed: false,
        }
        H.series.push(rec)
        ownedSeries.push(rec)
        const setData = series.setData as (d: unknown) => void
        series.setData = (d: unknown) => {
          rec.data = Array.isArray(d) ? [...d] : []
          setData(d)
        }
        // 价位线: 记下入参 + 之后有没有被 removePriceLine 撤掉(断言"关子图层只撤自己那两条")
        series.createPriceLine = (o: unknown) => {
          const opts = (o ?? {}) as Record<string, unknown>
          const line: RecPriceLine = {
            price: typeof opts.price === 'number' ? opts.price : undefined,
            title: typeof opts.title === 'string' ? opts.title : undefined,
            lineStyle: typeof opts.lineStyle === 'number' ? opts.lineStyle : undefined,
            color: typeof opts.color === 'string' ? opts.color : undefined,
            removed: false,
          }
          H.priceLines.push(line)
          ownedLines.push(line)
          return line as unknown as Record<string, never>
        }
        series.removePriceLine = (line: unknown) => {
          const rec2 = line as RecPriceLine | null
          if (rec2 && typeof rec2 === 'object' && 'removed' in rec2) rec2.removed = true
        }
        series.__rec = rec
        return series
      }
      chart.removeSeries = (series: unknown) => {
        const rec = (series as { __rec?: RecSeries } | null)?.__rec
        if (rec) rec.removed = true
      }
      chart.remove = () => {
        for (const rec of ownedSeries) rec.removed = true
        for (const line of ownedLines) line.removed = true
        ownedSeries.length = 0
        ownedLines.length = 0
      }
      return chart
    },
    createSeriesMarkers: (_series: unknown, markers?: unknown[]) => {
      H.markers = Array.isArray(markers) ? [...markers] : []
      return {
        setMarkers: (next: unknown[]) => {
          H.markers = Array.isArray(next) ? [...next] : []
        },
        detach: () => {},
      }
    },
  }
})

import KlineChart from '@sida/biz-ui/components/KlineChart'
import {
  buildChanCentreBand,
  buildChanSegmentLines,
  CHAN_CENTRE_LABEL,
  CHAN_SEGMENT_COLORS,
  chanCentreStatusText,
  normalizeChanOperationalCentre,
  normalizeChanSegments,
  type ChanOperationalCentre,
} from '@sida/biz-ui/lib/chan-layer'

const SEC = (day: string) => Date.parse(`${day}T00:00:00Z`) / 1000

const chanMarkers = () => (H.markers as Marker[]).filter((m) => typeof m.chanDay === 'string')
/** 线段 series: 用**导出常量里的颜色**识别(与笔的紫色刻意不同色)。 */
const segmentSeries = () =>
  H.series.filter((s) => s.type === 'Line' && s.options.color === CHAN_SEGMENT_COLORS.confirmed)
const candidateSegmentSeries = () =>
  H.series.filter((s) => s.type === 'Line' && String(s.options.color) === CHAN_SEGMENT_COLORS.candidate)
const penSeries = () => H.series.filter((s) => s.type === 'Line' && s.options.color === '#6d28d9')
/** 图上**还活着**的那批(假库保留已移除的记录, 断言"是否还在图上"必须过滤)。 */
const live = <T extends { removed: boolean }>(arr: T[]): T[] => arr.filter((s) => !s.removed)
/** 操作口径中枢价格带(按轴标签识别, 不受别的价位线影响)。 */
const centreLines = () => H.priceLines.filter((l) => (l.title ?? '').includes(CHAN_CENTRE_LABEL))

// ── 夹具: 8 根日K + 7 笔(6 确认 + 1 候选) + 2 确认线段 + 1 候选尾 + 1 blocked ──
const BARS: Bar[] = [
  { date: '2026-03-02', open: 30, high: 30.4, low: 28, close: 30.2, volume: 800 },
  { date: '2026-03-03', open: 30.2, high: 31.2, low: 30, close: 31, volume: 900 },
  { date: '2026-03-04', open: 31, high: 31.4, low: 29.4, close: 29.6, volume: 1000 },
  { date: '2026-03-05', open: 29.6, high: 32.2, low: 29.5, close: 32, volume: 1100 },
  { date: '2026-03-06', open: 32, high: 32.2, low: 30.4, close: 30.6, volume: 1000 },
  { date: '2026-03-09', open: 30.6, high: 33.6, low: 30.5, close: 33.4, volume: 1200 },
  { date: '2026-03-10', open: 33.4, high: 33.5, low: 30.9, close: 31.1, volume: 900 },
  { date: '2026-03-11', open: 31.1, high: 33.1, low: 31, close: 33, volume: 950 },
]

const FRACTALS = [
  { kind: 'bottom', status: 'confirmed', position_day: '2026-03-02', position_price: 28, confirmed_on: '2026-03-03' },
  { kind: 'top', status: 'confirmed', position_day: '2026-03-03', position_price: 31, confirmed_on: '2026-03-04' },
  { kind: 'bottom', status: 'confirmed', position_day: '2026-03-06', position_price: 30.5, confirmed_on: '2026-03-09' },
  { kind: 'top', status: 'confirmed', position_day: '2026-03-11', position_price: 33, confirmed_on: '2026-03-11' },
]

function pen(
  direction: 'up' | 'down',
  status: 'confirmed' | 'candidate',
  confirmedOn: string | null,
  start: { kind: 'top' | 'bottom'; day: string; price: number },
  end: { kind: 'top' | 'bottom'; day: string; price: number },
) {
  return {
    direction,
    status,
    confirmed_on: confirmedOn,
    kernel_version: 'chan-pen-v1',
    start: { kind: start.kind, position_day: start.day, price: start.price, status: 'confirmed', confirmed_on: null },
    end: { kind: end.kind, position_day: end.day, price: end.price, status: 'confirmed', confirmed_on: null },
  }
}

const PENS = [
  pen('up', 'confirmed', '2026-03-04', { kind: 'bottom', day: '2026-03-02', price: 28 }, { kind: 'top', day: '2026-03-03', price: 31 }),
  pen('down', 'confirmed', '2026-03-05', { kind: 'top', day: '2026-03-03', price: 31 }, { kind: 'bottom', day: '2026-03-04', price: 29.5 }),
  pen('up', 'confirmed', '2026-03-06', { kind: 'bottom', day: '2026-03-04', price: 29.5 }, { kind: 'top', day: '2026-03-05', price: 32 }),
  pen('down', 'confirmed', '2026-03-09', { kind: 'top', day: '2026-03-05', price: 32 }, { kind: 'bottom', day: '2026-03-06', price: 30.5 }),
  pen('up', 'confirmed', '2026-03-10', { kind: 'bottom', day: '2026-03-06', price: 30.5 }, { kind: 'top', day: '2026-03-09', price: 33.5 }),
  pen('down', 'confirmed', '2026-03-11', { kind: 'top', day: '2026-03-09', price: 33.5 }, { kind: 'bottom', day: '2026-03-10', price: 31 }),
  pen('up', 'candidate', null, { kind: 'bottom', day: '2026-03-10', price: 31 }, { kind: 'top', day: '2026-03-11', price: 33 }),
]

const SEGMENTS = {
  schema: 'sida-chan-segments-v1',
  kernel_version: 'chan-segment-v2',
  rules_version: 'chan-segment-v2',
  source_pen_kernel_version: 'chan-pen-v1',
  as_of_day: '2026-03-11',
  input_digest: SEGMENTS_DIGEST,
  min_pens: 3,
  confirmed: [
    {
      index: 0,
      status: 'confirmed',
      reason: 'first_case_no_gap',
      confirmed_on: '2026-03-06',
      kernel_version: 'chan-segment-v2',
      characteristic_elements: 3,
      geometry: {
        direction: 'up',
        pen_start_index: 0,
        pen_end_index: 2,
        pens_used: 3,
        start: { kind: 'bottom', position_day: '2026-03-02', price: 28, status: 'confirmed', confirmed_on: '2026-03-03' },
        end: { kind: 'top', position_day: '2026-03-05', price: 32, status: 'confirmed', confirmed_on: '2026-03-06' },
      },
      evidence: { pen_indices: [0, 1, 2, 3], confirmed_on: '2026-03-06', fractal_elements: [] },
    },
    {
      index: 1,
      status: 'confirmed',
      reason: 'first_case_no_gap',
      confirmed_on: '2026-03-11',
      kernel_version: 'chan-segment-v2',
      characteristic_elements: 3,
      geometry: {
        direction: 'down',
        pen_start_index: 3,
        pen_end_index: 5,
        pens_used: 3,
        start: { kind: 'top', position_day: '2026-03-05', price: 32, status: 'confirmed', confirmed_on: '2026-03-06' },
        end: { kind: 'bottom', position_day: '2026-03-10', price: 31, status: 'confirmed', confirmed_on: '2026-03-11' },
      },
      evidence: { pen_indices: [3, 4, 5], confirmed_on: '2026-03-11', fractal_elements: [] },
    },
  ],
  candidate_tail: {
    index: 2,
    status: 'candidate',
    reason: 'first_case_no_gap',
    confirmed_on: null,
    driftable: true,
    note: '尾部候选线段: 终点价格仍可漂移, 不是已确认几何, 不得当作中枢输入',
    kernel_version: 'chan-segment-v2',
    characteristic_elements: 1,
    geometry: {
      direction: 'up',
      pen_start_index: 6,
      pen_end_index: 6,
      pens_used: 1,
      start: { kind: 'bottom', position_day: '2026-03-10', price: 31, status: 'confirmed', confirmed_on: '2026-03-11' },
      end: { kind: 'top', position_day: '2026-03-11', price: 33, status: 'candidate', confirmed_on: null },
    },
    evidence: { pen_indices: [6], confirmed_on: null, fractal_elements: [] },
  },
  pending: [],
  blocked: [
    {
      index: 3,
      status: 'blocked',
      reason: 'characteristic_initial_containment',
      direction: 'down',
      pen_start_index: 7,
      pen_end_index: 8,
      pens_used: 2,
      geometry_withheld: true,
      note: '内核拒绝出版该线段(blocked): 不提供价格区间, 不得作为几何/中枢输入',
    },
  ],
  rejections: [],
  counts: { confirmed: 2, candidate: 1, blocked: 1, pending: 0, total: 4 },
  note: '线段=chan-segment-v2 ...',
}

function centreBlock(over: Record<string, unknown> = {}) {
  return {
    schema: 'sida-chan-operational-centre-v1',
    adapter_version: 'chan-centre-adapter-v1',
    input_kind: 'pen_snapshot',
    centre_input_kind: 'segment_kernel_confirmed_prefix',
    segments_engine: 'chan-segment-v2',
    basis: 'segment_a0',
    strictness: 'operational_not_strict',
    status: 'centre',
    chain_complete: false,
    limitations: ['case2_gap_segments_not_implemented_upstream'],
    as_of_day: '2026-03-11',
    absence_reason: null,
    confirmed_prefix_length: 2,
    total_segments: 4,
    zone: { zd: 29.5, zg: 32, width: 2.5 },
    segment_indices: [0, 1],
    structure_start_day: '2026-03-02',
    structure_start_price: 28,
    confirmed_on: '2026-03-11',
    visible_on: '2026-03-11',
    lifecycle_status: 'confirmed',
    issues: [
      {
        reason: 'characteristic_initial_containment',
        detail: '第 4 条线段被内核拒绝出版',
        blocking: true,
        segment_index: 3,
        pen_index: 11,
      },
    ],
    source: {
      symbol: '600519',
      adjust: 'unknown',
      pen_digest: SEGMENTS_DIGEST,
      segment_kernel: 'chan-segment-v2',
      pen_kernel: 'chan-pen-v1',
    },
    status_note: '已由前 3 条已确认线段测出操作口径区间(zd/zg)',
    label_note: '字段名 operational_centre 指 basis=segment_a0 ... 不是「中枢」',
    ...over,
  }
}

function klinesResponse(symbol: string, opts: { digest?: string; bars?: Bar[] } = {}) {
  const bars = opts.bars ?? BARS
  return {
    symbol,
    market: 'CN',
    interval: '1d',
    is_index: false,
    klines: bars,
    source: 'pg',
    chan_snapshot: {
      schema: 'sida-chan-snapshot-v1',
      rules_version: 'chan-fractal-v1',
      instrument: 'stock',
      interval: '1d',
      bars: bars.length,
      as_of_day: bars.length ? bars[bars.length - 1].date : null,
      digest: opts.digest ?? DIGEST_A,
    },
  }
}

function structureResponse(over: Record<string, unknown> = {}) {
  return {
    symbol: '600519',
    market: 'CN',
    instrument: 'stock',
    interval: '1d',
    rules_version: 'chan-fractal-v1',
    snapshot_digest: DIGEST_A,
    as_of_day: '2026-03-11',
    omitted_latest_reason: null,
    source: 'pg',
    adjustment: 'unknown',
    finality: 'unverified',
    fractals: FRACTALS,
    pens: PENS,
    pen_rules_version: 'chan-pen-v1',
    pen_counts: { total: 7, confirmed: 6, candidate: 1, blocked: 0 },
    segments: SEGMENTS,
    operational_centre: centreBlock(),
    not_implemented: ['segment_case2_gap', 'strict_centre_18th_lesson_definition', 'buy_sell_point'],
    ...over,
  }
}

async function waitForChartReady() {
  await waitFor(() => expect(screen.getByText(/根K线/)).toBeTruthy(), { timeout: 5000 })
}

/** 打开主图层（本地开关），等结构就绪（图例出现）。 */
async function mountWithChan(structure: Record<string, unknown> = structureResponse()) {
  H.klines = () => Promise.resolve(klinesResponse('600519'))
  H.structure = () => Promise.resolve(structure)
  const utils = render(<KlineChart symbol="600519" market="CN" instrument="stock" layersVisible={{ chan: true }} />)
  await waitForChartReady()
  await screen.findByTestId('chan-legend', {}, { timeout: 5000 })
  return utils
}

beforeEach(() => {
  H.calls.length = 0
  H.markers = []
  H.series.length = 0
  H.priceLines.length = 0
  H.klines = null
  H.structure = null
})

afterEach(() => {
  cleanup()
})

describe('KlineChart 缠论线段子图层', () => {
  it('子图层默认关: 只开分型/笔时没有任何线段 series, 也没有中枢价位线', async () => {
    await mountWithChan()
    // 笔照画(主图层不动; 6 条已确认笔, 候选笔是同色压淡的另一条), 线段/中枢子开关默认关 → 一条都没有
    await waitFor(() => expect(live(penSeries()).length).toBe(6), { timeout: 5000 })
    expect(segmentSeries()).toHaveLength(0)
    expect(candidateSegmentSeries()).toHaveLength(0)
    expect(centreLines()).toHaveLength(0)
    // 两个子开关都还没激活(默认关)
    expect(screen.getByTestId('chan-segment-toggle').getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByTestId('chan-centre-toggle').getAttribute('aria-pressed')).toBe('false')
    // 分型 marker 已在图上(默认关的子图层不许影响它)
    expect(chanMarkers().length).toBeGreaterThan(0)
  })

  it('打开线段: 已确认=粗实线(lineWidth 2), 候选=细虚线且明写可漂移; blocked 一段线都不画', async () => {
    await mountWithChan()
    await waitFor(() => expect(live(penSeries()).length).toBe(6), { timeout: 5000 })

    fireEvent.click(screen.getByTestId('chan-segment-toggle'))
    await waitFor(() => expect(live(segmentSeries())).toHaveLength(2), { timeout: 5000 })
    await waitFor(() => expect(live(candidateSegmentSeries())).toHaveLength(1), { timeout: 5000 })

    // 已确认线段: 实线 + **比笔粗一档**(lineWidth 2), 端点逐点等于服务端端点(不插值)
    const [seg0, seg1] = live(segmentSeries())
    expect(seg0.options.lineStyle).toBe(0)
    expect(seg0.options.lineWidth).toBe(2)
    expect(seg0.data).toEqual([
      { time: SEC('2026-03-02'), value: 28 },
      { time: SEC('2026-03-05'), value: 32 },
    ])
    expect(seg1.options.lineStyle).toBe(0)
    expect(seg1.options.lineWidth).toBe(2)
    expect(seg1.data).toEqual([
      { time: SEC('2026-03-05'), value: 32 },
      { time: SEC('2026-03-10'), value: 31 },
    ])

    // 候选尾: 细虚线 + 同色压淡; 只有两个端点(不跨日造点)
    const [tail] = live(candidateSegmentSeries())
    expect(tail.options.lineStyle).toBe(2)
    expect(tail.options.lineWidth).toBe(1)
    expect(String(tail.options.color)).toContain('rgba(')
    expect(tail.data).toEqual([
      { time: SEC('2026-03-10'), value: 31 },
      { time: SEC('2026-03-11'), value: 33 },
    ])

    // blocked 线段(笔区间 7..8, 服务端未给价格几何): 图上只有 3 条线, 没有第四条。
    // 断言"活着的"而不是历史创建数: 打开子图层会把主图切到更长回看(见加长回看用例),
    // 期间旧一批被显式移除 —— 残留才算错。
    expect(live(segmentSeries()).length + live(candidateSegmentSeries()).length).toBe(3)
    for (const s of [...segmentSeries(), ...candidateSegmentSeries()]) {
      expect((s.data as Array<{ time: number }>).length).toBe(2) // 只有两个端点
    }

    // 图例: 已确认/候选分列, 候选明写"可漂移"; blocked 只计数、不给线型样例
    const legend = screen.getByTestId('chan-legend')
    const segmentLegend = screen.getByTestId('chan-segment-legend')
    expect(segmentLegend.textContent).toContain('已确认(粗实线) 2')
    expect(segmentLegend.textContent).toContain('终点可漂移')
    expect(legend.textContent).toContain('blocked 线段 1 段（不绘·服务端未给价格几何）')
    // 无买卖语义(图例里只允许出现"非买卖点"这类否定语, 不许出现买入/卖出动作词)
    expect(legend.textContent).not.toContain('买入')
    expect(legend.textContent).not.toContain('卖出')
    expect(legend.textContent).not.toContain('目标价')
  })

  it('关线段子图层: 线段 series 全部移除, 分型记号/笔线/中枢价位线一条不少', async () => {
    H.klines = () => Promise.resolve(klinesResponse('600519'))
    H.structure = () => Promise.resolve(structureResponse())
    const { rerender } = render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true, chanSegments: true, chanCentre: true }}
      />,
    )
    await waitFor(() => expect(live(segmentSeries())).toHaveLength(2), { timeout: 5000 })
    await waitFor(() => expect(live(centreLines())).toHaveLength(2), { timeout: 5000 })

    const drawnSegments = [...live(segmentSeries()), ...live(candidateSegmentSeries())]
    const markersBefore = chanMarkers().length
    const pensBefore = live(penSeries()).length
    const centreBefore = live(centreLines()).length
    expect(markersBefore).toBeGreaterThan(0)

    rerender(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true, chanSegments: false, chanCentre: true }}
      />,
    )
    await waitFor(() => expect(live(segmentSeries())).toHaveLength(0), { timeout: 5000 })
    await waitFor(() => expect(live(candidateSegmentSeries())).toHaveLength(0), { timeout: 5000 })
    // 每条旧线段都被显式移除(不是"留在那儿但看不见")
    for (const s of drawnSegments) expect(s.removed).toBe(true)
    // 分型 marker / 笔线 / 中枢价位线都没被连带删
    expect(chanMarkers()).toHaveLength(markersBefore)
    expect(live(penSeries())).toHaveLength(pensBefore)
    expect(live(centreLines())).toHaveLength(centreBefore)
  })

  it('操作口径中枢: zd/zg 两条水平线且标签写"操作口径·非严格中枢"; 关掉只撤自己那两条', async () => {
    H.klines = () => Promise.resolve(klinesResponse('600519'))
    H.structure = () => Promise.resolve(structureResponse())
    const { rerender } = render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true, chanCentre: true }}
      />,
    )
    await waitFor(() => expect(live(centreLines())).toHaveLength(2), { timeout: 5000 })

    const [zd, zg] = live(centreLines())
    expect(zd.price).toBe(29.5)
    expect(zg.price).toBe(32)
    expect(zd.title).toBe(`${CHAN_CENTRE_LABEL} zd`)
    expect(zg.title).toBe(`${CHAN_CENTRE_LABEL} zg`)
    expect(zd.lineStyle).toBe(2) // 虚线: 操作口径, 不是已确认结构
    // 标签里不许出现"买卖/目标价"这类更强的结论
    for (const line of live(centreLines())) {
      expect(line.title).toContain('非严格')
      expect(line.title).not.toMatch(/买|卖|目标价/)
    }
    // 线段子图层没开: 一根线段都不画(两个子开关互不影响)
    expect(segmentSeries()).toHaveLength(0)
    // 图例透出 basis/strictness 与"非买卖点/非交易信号"
    const legend = screen.getByTestId('chan-centre-legend')
    expect(legend.textContent).toContain('basis=segment_a0')
    expect(legend.textContent).toContain('非买卖点')
    expect(legend.textContent).toContain('交易信号')

    const lines = live(centreLines())
    rerender(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true, chanCentre: false }}
      />,
    )
    await waitFor(() => expect(live(centreLines())).toHaveLength(0), { timeout: 5000 })
    for (const l of lines) expect(l.removed).toBe(true)
    // 笔线照旧
    await waitFor(() => expect(live(penSeries()).length).toBe(6), { timeout: 5000 })
  })

  it('跨快照(线段 as_of_day 与结构不一致): 线段整批不画、中枢也不画, 图例给原因码', async () => {
    await mountWithChan(
      structureResponse({
        segments: { ...SEGMENTS, as_of_day: '2026-03-09' },
      }),
    )
    fireEvent.click(screen.getByTestId('chan-segment-toggle'))
    fireEvent.click(screen.getByTestId('chan-centre-toggle'))

    // 等图例落定(结构 ready)后断言"一条都没画"
    const legend = await screen.findByTestId('chan-legend', {}, { timeout: 5000 })
    expect(legend.textContent).toContain('segments-cross-snapshot')
    await waitFor(() => expect(screen.getByTestId('chan-centre-legend').textContent).toContain('centre-cross-snapshot'), {
      timeout: 5000,
    })
    expect(segmentSeries()).toHaveLength(0)
    expect(candidateSegmentSeries()).toHaveLength(0)
    expect(centreLines()).toHaveLength(0)
  })

  it('中枢 pen digest 与线段块不是同一份(混批): 不画带, 图例说明原因', async () => {
    await mountWithChan(
      structureResponse({
        operational_centre: centreBlock({
          source: { ...centreBlock().source, pen_digest: DIGEST_B },
        }),
      }),
    )
    fireEvent.click(screen.getByTestId('chan-centre-toggle'))
    await waitFor(
      () => expect(screen.getByTestId('chan-centre-legend').textContent).toContain('centre-cross-snapshot'),
      { timeout: 5000 },
    )
    expect(centreLines()).toHaveLength(0)
  })

  it('not_enough_confirmed_segments 不得说成"无中枢"; no_centre 才是实测无交集', async () => {
    // ① 不够: 不画带, 措辞必须区分于"没有"
    await mountWithChan(
      structureResponse({
        segments: {
          ...SEGMENTS,
          confirmed: [],
          candidate_tail: null,
          blocked: [],
          counts: { confirmed: 0, candidate: 0, blocked: 0, pending: 0, total: 0 },
        },
        operational_centre: centreBlock({
          status: 'not_enough_confirmed_segments',
          zone: null,
          confirmed_prefix_length: 0,
          total_segments: 0,
          absence_reason: null,
          segment_indices: null,
          confirmed_on: null,
          visible_on: null,
          issues: [],
        }),
      }),
    )
    fireEvent.click(screen.getByTestId('chan-centre-toggle'))
    await waitFor(() => expect(screen.getByTestId('chan-centre-legend')).toBeTruthy(), { timeout: 5000 })
    const notEnough = screen.getByTestId('chan-centre-legend').textContent ?? ''
    expect(notEnough).toContain('not_enough_confirmed_segments')
    expect(notEnough).toContain('不能据此说中枢不存在')
    expect(notEnough).not.toContain('无中枢')
    expect(notEnough).not.toContain('实测无交集')
    expect(centreLines()).toHaveLength(0)
  })

  it('结构失败/卸载: 线段与中枢价位线都不留在图上(自身图层清空, 不静默残留)', async () => {
    H.klines = (url) =>
      Promise.resolve(url.includes('000002') ? klinesResponse('000002', { digest: DIGEST_B }) : klinesResponse('600519'))
    H.structure = (url) => {
      if (url.includes('snapshot=' + DIGEST_B)) {
        // 新标的的结构取数失败 → 图层必须进 error，旧几何一条都不许留着
        return Promise.reject(Object.assign(new Error('upstream boom'), { status: 503 }))
      }
      return Promise.resolve(structureResponse())
    }
    const view = render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true, chanSegments: true, chanCentre: true }}
      />,
    )
    await waitFor(() => expect(live(segmentSeries())).toHaveLength(2), { timeout: 5000 })
    const segs = [...live(segmentSeries()), ...live(candidateSegmentSeries())]
    const lines = live(centreLines())
    expect(lines).toHaveLength(2)

    // ① 取数失败: 结构进 error phase，先撤几何再说明失败(不显示上一次的结论)
    view.rerender(
      <KlineChart
        symbol="000002"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true, chanSegments: true, chanCentre: true }}
      />,
    )
    await waitFor(() => expect(live(segmentSeries())).toHaveLength(0), { timeout: 5000 })
    for (const s of segs) expect(s.removed).toBe(true)
    await waitFor(() => expect(live(centreLines())).toHaveLength(0), { timeout: 5000 })
    for (const l of lines) expect(l.removed).toBe(true)
    expect(await screen.findByTestId('chan-status')).toBeTruthy()

    // ② 卸载: 图表销毁 → 线段/价位线随图消失(引用也清空, 不留指向死图的句柄)
    H.structure = () => Promise.resolve(structureResponse())
    view.rerender(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true, chanSegments: true, chanCentre: true }}
      />,
    )
    await waitFor(() => expect(live(segmentSeries())).toHaveLength(2), { timeout: 5000 })
    const segs2 = [...live(segmentSeries()), ...live(candidateSegmentSeries())]
    const lines2 = live(centreLines())
    view.unmount()
    for (const s of segs2) expect(s.removed).toBe(true)
    for (const l of lines2) expect(l.removed).toBe(true)
  })

  it('切股: 旧图的线段与中枢价位线立即撤掉, 新图按新摘要重建', async () => {
    H.klines = (url) =>
      Promise.resolve(url.includes('000001') ? klinesResponse('000001', { digest: DIGEST_B }) : klinesResponse('600519'))
    // 切股后的结构必须**整份**都属于新标的: 中枢块自述的 source.symbol 也要跟着换,
    // 否则就是"拿 A 的中枢画在 B 的图上"(正常实现会把这种混批拒掉, 这里造的是合法响应)。
    H.structure = (url) =>
      Promise.resolve(
        url.includes('snapshot=' + DIGEST_B)
          ? structureResponse({
              symbol: '000001',
              snapshot_digest: DIGEST_B,
              operational_centre: centreBlock({
                source: { ...centreBlock().source, symbol: '000001' },
              }),
            })
          : structureResponse(),
      )
    const { rerender } = render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true, chanSegments: true, chanCentre: true }}
      />,
    )
    await waitFor(() => expect(live(segmentSeries())).toHaveLength(2), { timeout: 5000 })
    const oldSegments = [...live(segmentSeries()), ...live(candidateSegmentSeries())]
    const oldCentre = live(centreLines())
    expect(oldSegments.length).toBe(3)
    expect(oldCentre.length).toBe(2)

    rerender(
      <KlineChart
        symbol="000001"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true, chanSegments: true, chanCentre: true }}
      />,
    )
    // 新图的线段到位(旧的那些必须已被移除)
    await waitFor(() => expect(live(segmentSeries())).toHaveLength(2), { timeout: 5000 })
    for (const s of oldSegments) expect(s.removed).toBe(true)
    await waitFor(() => expect(live(centreLines())).toHaveLength(2), { timeout: 5000 })
    for (const l of oldCentre) expect(l.removed).toBe(true)
  })
})

describe('线段/中枢子图层打开时加长回看(2026-11-18)', () => {
  const mainKlineCalls = () => H.calls.filter((u) => u.startsWith('/klines/600519?'))

  it('打开线段 → 同一张图改按 800 天请求(图例写明), 关掉回到 initialDays', async () => {
    await mountWithChan()
    // 默认视图(只开分型/笔): 仍按 initialDays
    expect(mainKlineCalls()[0]).toContain('days=120')
    expect(screen.queryByTestId('chan-lookback-note')).toBeNull()

    fireEvent.click(screen.getByTestId('chan-segment-toggle'))
    // 主图加长到 800 个日历天(约 500 根日 K; 不是另取一份结构数据: 摘要仍由这条响应的 chan_snapshot 绑定)
    await waitFor(() => expect(mainKlineCalls().some((u) => u.includes('days=800'))).toBe(true), { timeout: 5000 })
    // 结构请求跟着同一份快照/同一批天数
    await waitFor(
      () => expect(H.calls.some((u) => u.includes('/chan-structure') && u.includes('days=800'))).toBe(true),
      { timeout: 5000 },
    )
    const note = await screen.findByTestId('chan-lookback-note', {}, { timeout: 5000 })
    expect(note.textContent).toContain('已经加载近 800 天'.replace('已经', '已'))
    expect(note.textContent).toContain('默认 120 天')

    // 关掉子图层 → 回到初始天数, 图例上的说明同步消失
    fireEvent.click(screen.getByTestId('chan-segment-toggle'))
    await waitFor(
      () => expect(mainKlineCalls()[mainKlineCalls().length - 1]).toContain('days=120'),
      { timeout: 5000 },
    )
    await waitFor(() => expect(screen.queryByTestId('chan-lookback-note')).toBeNull(), { timeout: 5000 })
  })

  it('中枢子图层同样触发加长(两个子图层任一打开即可)', async () => {
    H.klines = () => Promise.resolve(klinesResponse('600519'))
    H.structure = () => Promise.resolve(structureResponse())
    render(<KlineChart symbol="600519" market="CN" instrument="stock" layersVisible={{ chan: true, chanCentre: true }} />)
    await waitFor(() => expect(mainKlineCalls().some((u) => u.includes('days=800'))).toBe(true), { timeout: 5000 })
    await waitFor(() => expect(live(centreLines())).toHaveLength(2), { timeout: 5000 })
  })
})

describe('chan-layer 线段/中枢纯函数', () => {
  const ctx = { asOfDay: '2026-03-11', symbol: '600519', penKernel: 'chan-pen-v1' }

  it('线段: blocked 带价格几何 / 计数不自洽 / confirmed 链断裂 → 整批拒收(不挑着画)', () => {
    const leaked = normalizeChanSegments(
      {
        ...SEGMENTS,
        blocked: [{ ...SEGMENTS.blocked[0], geometry: { direction: 'down', pen_start_index: 7, pen_end_index: 8 } }],
      },
      ctx,
    )
    expect(leaked.rejectReason).toBe('segment-blocked-has-geometry')
    expect(leaked.confirmed).toHaveLength(0)

    const badCounts = normalizeChanSegments({ ...SEGMENTS, counts: { ...SEGMENTS.counts, confirmed: 3 } }, ctx)
    expect(badCounts.rejectReason).toBe('segments-counts-inconsistent')
    expect(badCounts.confirmed).toHaveLength(0)

    const brokenChain = normalizeChanSegments(
      {
        ...SEGMENTS,
        confirmed: [
          SEGMENTS.confirmed[0],
          {
            ...SEGMENTS.confirmed[1],
            geometry: { ...SEGMENTS.confirmed[1].geometry, pen_start_index: 4 },
          },
        ],
      },
      ctx,
    )
    expect(brokenChain.rejectReason).toBe('segment-confirmed-chain-broken')
    expect(brokenChain.confirmed).toHaveLength(0)

    // pending 只计数、不参与几何; blocked 同理(没有任何价格几何流到可画集合里)
    const withPending = normalizeChanSegments(
      {
        ...SEGMENTS,
        pending: [{ status: 'pending', reason: 'no_break', direction: 'up', pen_start_index: 9, note: 'pending 不是线段' }],
        counts: { ...SEGMENTS.counts, pending: 1 },
      },
      ctx,
    )
    expect(withPending.rejectReason).toBeNull()
    expect(withPending.pending).toBe(1)
    expect(withPending.confirmed).toHaveLength(2)
    expect(withPending.candidateTail !== null).toBe(true)

    const ok = normalizeChanSegments(SEGMENTS, ctx)
    expect(ok.rejectReason).toBeNull()
    expect(ok.confirmed.map((s) => s.status)).toEqual(['confirmed', 'confirmed'])
    expect(ok.candidateTail?.driftable).toBe(true)
    expect(ok.blocked).toBe(1)
    expect(ok.pending).toBe(0)
    expect(ok.inputDigest).toBe(SEGMENTS_DIGEST)
  })

  it('线段: schema 不认识 / 截止日对不上 / 未声明可漂移的候选 → 不画', () => {
    expect(normalizeChanSegments({ ...SEGMENTS, schema: 'sida-chan-segments-v9' }, ctx).rejectReason).toBe(
      'segments-schema-unknown',
    )
    expect(normalizeChanSegments(SEGMENTS, { ...ctx, asOfDay: '2026-03-10' }).rejectReason).toBe(
      'segments-cross-snapshot',
    )
    expect(normalizeChanSegments(SEGMENTS, { ...ctx, penKernel: 'chan-pen-v2' }).rejectReason).toBe(
      'segments-cross-snapshot',
    )
    const noDrift = normalizeChanSegments(
      { ...SEGMENTS, candidate_tail: { ...SEGMENTS.candidate_tail, driftable: false } },
      ctx,
    )
    expect(noDrift.candidateTail).toBeNull()
    expect(noDrift.dropped).toBe(1)
  })

  it('中枢: 标签漂移 / 状态未知 / 区间与状态矛盾 → 拒画(不把操作口径升级成严格中枢)', () => {
    const labelCtx = { asOfDay: '2026-03-11', symbol: '600519', segmentsDigest: SEGMENTS_DIGEST }
    expect(
      normalizeChanOperationalCentre(centreBlock({ strictness: 'strict' }), labelCtx).rejectReason,
    ).toBe('centre-label-mismatch')
    expect(normalizeChanOperationalCentre(centreBlock({ status: 'zhongshu' }), labelCtx).rejectReason).toBe(
      'centre-unknown-status',
    )
    expect(normalizeChanOperationalCentre(centreBlock({ zone: { zd: 32, zg: 29.5 } }), labelCtx).rejectReason).toBe(
      'centre-zone-invalid',
    )
    // 标的/截止日对不上 = 拿别的图的中枢往这张图上画, 一律不画
    expect(
      normalizeChanOperationalCentre(
        centreBlock({ source: { ...centreBlock().source, symbol: '000001' } }),
        labelCtx,
      ).rejectReason,
    ).toBe('centre-cross-snapshot')
    expect(
      normalizeChanOperationalCentre(centreBlock({ as_of_day: '2026-03-10' }), labelCtx).rejectReason,
    ).toBe('centre-cross-snapshot')
    expect(
      normalizeChanOperationalCentre(
        centreBlock({ status: 'no_centre', zone: { zd: 29.5, zg: 32 } }),
        labelCtx,
      ).rejectReason,
    ).toBe('centre-zone-contradicts-status')
    expect(
      normalizeChanOperationalCentre(centreBlock({ zone: null }), labelCtx).rejectReason,
    ).toBe('centre-zone-contradicts-status')

    const ok = normalizeChanOperationalCentre(centreBlock(), labelCtx)
    expect(ok.rejectReason).toBeNull()
    expect(ok.centre?.zone).toEqual({ zd: 29.5, zg: 32 })
    expect(ok.centre?.basis).toBe('segment_a0')
    expect(ok.centre?.strictness).toBe('operational_not_strict')
    // visible_on 允许缺失(字段可能改名): 缺了就是 null, 不猜
    const noVisible = normalizeChanOperationalCentre(centreBlock({ visible_on: undefined }), labelCtx)
    expect(noVisible.centre?.visibleOn).toBeNull()
  })

  it('中枢措辞: not_enough 与 no_centre 各说各的, 都不带买卖语义', () => {
    const labelCtx = { asOfDay: '2026-03-11', symbol: '600519', segmentsDigest: SEGMENTS_DIGEST }
    const notEnough = normalizeChanOperationalCentre(
      centreBlock({ status: 'not_enough_confirmed_segments', zone: null }),
      labelCtx,
    ).centre as ChanOperationalCentre
    const noCentre = normalizeChanOperationalCentre(
      centreBlock({ status: 'no_centre', zone: null, absence_reason: 'no_intersection' }),
      labelCtx,
    ).centre as ChanOperationalCentre
    const blocked = normalizeChanOperationalCentre(
      centreBlock({ status: 'blocked_before_centre', zone: null }),
      labelCtx,
    ).centre as ChanOperationalCentre

    const t1 = chanCentreStatusText(notEnough)
    const t2 = chanCentreStatusText(noCentre)
    const t3 = chanCentreStatusText(blocked)
    expect(t1).toContain('not_enough_confirmed_segments')
    expect(t1).not.toContain('无中枢')
    expect(t2).toContain('实测无交集')
    expect(t2).toContain('measured absence')
    expect(t3).toContain('blocked_before_centre')
    for (const t of [t1, t2, t3]) {
      expect(t).toContain(CHAN_CENTRE_LABEL)
      expect(t).not.toMatch(/买入|卖出|买点|卖点|目标价/)
    }
  })

  it('几何: 只有 status=centre 才产出价格带; 端点日不在图上 → 整段跳过(不贴到最近的柱子)', () => {
    const colours = CHAN_SEGMENT_COLORS
    const band = buildChanCentreBand(
      {
        status: 'centre',
        basis: 'segment_a0',
        strictness: 'operational_not_strict',
        chainComplete: false,
        zone: { zd: 29.5, zg: 32 },
        issues: [],
        limitations: [],
        confirmedPrefixLength: 2,
        totalSegments: 4,
        asOfDay: '2026-03-11',
        visibleOn: null,
        statusNote: null,
      },
      { colors: colours },
    )
    expect(band?.lines.map((l) => [l.price, l.role])).toEqual([
      [29.5, 'zd'],
      [32, 'zg'],
    ])
    expect(band?.dashed).toBe(true)
    const noCentreBand = buildChanCentreBand(
      {
        status: 'no_centre',
        basis: 'segment_a0',
        strictness: 'operational_not_strict',
        chainComplete: true,
        zone: null,
        issues: [],
        limitations: [],
        confirmedPrefixLength: 2,
        totalSegments: 2,
        asOfDay: '2026-03-11',
        visibleOn: null,
        statusNote: null,
      },
      { colors: colours },
    )
    expect(noCentreBand).toBeNull()

    const batch = normalizeChanSegments(SEGMENTS, ctx)
    const onlyFirstDay = new Set(['2026-03-02', '2026-03-11'])
    const drawn = buildChanSegmentLines(batch, { barDays: onlyFirstDay, colors: colours })
    // 两段已确认的终点日(03-05/03-10)都不在图上 → 全部跳过, 一条都不"贴过去"
    expect(drawn.segments).toHaveLength(0)
    expect(drawn.skipped).toHaveLength(3)
    expect(drawn.skipped.every((s) => s.reason === 'endpoint-day-not-on-chart')).toBe(true)
  })
})
