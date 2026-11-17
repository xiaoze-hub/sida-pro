// @vitest-environment jsdom
//
// 缠论分型图层（2026-11-17）—— **组件侧接线 + 快照绑定 + 竞态** 回归（渲染真 KlineChart）。
//
// 纯函数口径（清洗/审计/记号）在文件末尾的 `chan-layer 纯函数` 分组里钉住；
// 这里钉住"图层怎么挂的、什么时候不挂"，全部用**真组件 + 假 canvas 层**跑：
//   1. 默认关：一个 /chan-structure 请求都不发（不给首屏加请求）；
//   2. 打开：带 `snapshot=<图表摘要>`、`instrument`、`interval=1d`、`days` 请求；
//      只画 confirmed/candidate（blocked 不画），且视觉可区分、**无买卖语义**；
//   3. 关掉：认顿记号消失，但**别人的记号还在**（共用同一个 marker plugin，不许连带删除）；
//   4. 切股：旧结构清掉、迟到响应丢弃（序列号 + abort 双保险）、摘要换成新图的；
//   5. 409 / 回传摘要不一致 → 拒绝绘制且用户可见；
//   6. 图表响应缺摘要 → 不请求、说明原因；
//   7. 原 K 线被前端渲染链改写（重复日）→ 拒绝结构并说明原因；
//   8. 身份不可靠（未传 instrument）→ 按钮禁用、不发请求。
//
// 图表库被 vite.config.ts 的 test.alias 换成无副作用存根；本文件额外包一层
// `createSeriesMarkers`，把"图层真正收到的 marker 列表"记下来（canvas 上没有 DOM 可查）。
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

type Bar = { date: string; open: number; high: number; low: number; close: number; volume?: number }
type Marker = Record<string, unknown> & { chanDay?: string; chanStatus?: string; text?: string }

/**
 * 假图表库记下来的一个 series（2026-11-17 笔图层）。
 *
 * 笔画在 canvas 上、DOM 里查不到，唯一能核对的真实产物就是"组件究竟往图表里加了哪些
 * series、每个 series 收到什么数据/样式、最后有没有被移除"。所以这里把 addSeries 的
 * (类型, 选项) 与 setData 的入参原样记录下来 —— 断言直接打在组件真调用上。
 */
type RecSeries = {
  /** `LineSeries`/`CandlestickSeries` … 的类型标记 */
  type: string
  options: Record<string, unknown>
  data: unknown[]
  removed: boolean
}

/** 假后端的可编程面：每个用例自己装 `klines` / `structure` 两个 handler。 */
const H = vi.hoisted(() => ({
  calls: [] as string[],
  /** fetchAPI 收到的 (url, options) —— 用来断言"重读图表"真的绕过了缓存 */
  opts: [] as Array<{ url: string; options?: Record<string, unknown> }>,
  /** 图层最后一次真正收到的 marker 列表（createSeriesMarkers 初值 + 每次 setMarkers） */
  markers: [] as unknown[],
  /** 图表里创建过的**全部** series（含蜡烛/均线/笔 …），按创建顺序 */
  series: [] as Array<{ type: string; options: Record<string, unknown>; data: unknown[]; removed: boolean }>,
  klines: null as null | ((url: string) => Promise<unknown>),
  structure: null as null | ((url: string) => Promise<unknown>),
}))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string, options?: Record<string, unknown>) => {
    const u = String(url)
    H.calls.push(u)
    H.opts.push({ url: u, options })
    if (u.includes('/chan-structure')) {
      return H.structure ? H.structure(u) : Promise.resolve({})
    }
    if (u.includes('/klines/') && u.includes('?')) {
      return H.klines ? H.klines(u) : Promise.resolve({ klines: [] })
    }
    // summary / patterns / wyckoff / chart-notes / scenario / indicators … 一律空响应:
    // 本用例只关心缠论图层, 其它图层保持"无数据"即可。
    return Promise.resolve({})
  }),
  fetchScenario: vi.fn(() => Promise.resolve(null)),
}))

/**
 * 结构请求超时从 20s 压到 120ms: 超时用例必须**真的**能等到"挂住"那一刻, 不能靠等 20s。
 * 只改这个常量, 其余纯函数/文案/状态机全部用真实实现(不伪造判定)。
 */
vi.mock('@sida/biz-ui/lib/chan-layer', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return { ...actual, CHAN_STRUCTURE_TIMEOUT_MS: 120 }
})


vi.mock('lightweight-charts', async () => {
  const stub = await import('../../tests/stubs/lightweight-charts')
  const base = stub as unknown as {
    createChart: () => Record<string, unknown>
    LineStyle: Record<string, number>
  }
  return {
    ...base,
    LineStyle: { ...base.LineStyle, Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3 },
    // 记录"组件真的往图表里加了什么"：类型 + 选项 + setData 入参 + 是否被移除。
    createChart: () => {
      const chart = base.createChart()
      /** 本图实例名下的 series: `chart.remove()` 销毁整张图时它们一起消失(与真库一致) */
      const owned: RecSeries[] = []
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
        owned.push(rec)
        const setData = series.setData as (d: unknown) => void
        series.setData = (d: unknown) => {
          rec.data = Array.isArray(d) ? [...d] : []
          setData(d)
        }
        series.__rec = rec
        return series
      }
      chart.removeSeries = (series: unknown) => {
        const rec = (series as { __rec?: RecSeries } | null)?.__rec
        if (rec) rec.removed = true
      }
      // 真 `chart.remove()` 销毁图表 → 图上的 series 全部不存在了。假库必须同样记账,
      // 否则"重建图表后旧笔线还在不在"这类断言会看到已随旧图销毁的幽灵记录。
      chart.remove = () => {
        for (const rec of owned) rec.removed = true
        owned.length = 0
      }
      return chart
    },
    createSeriesMarkers: (_series: unknown, markers?: unknown[]) => {
      // 组件每次整批替换: 记"最后一次"才是图上现在真实存在的那批
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

/** 图上现在的缠论记号（按 marker 上挂的溯源字段识别）。 */
const chanMarkers = () => (H.markers as Marker[]).filter((m) => typeof m.chanDay === 'string')

/** 结构端点的请求 URL（没有则 null）。 */
const structureCall = () => H.calls.find((u) => u.includes('/chan-structure')) ?? null

const BARS: Bar[] = [
  { date: '2026-03-05', open: 32, high: 33, low: 31, close: 32.93, volume: 1000 },
  { date: '2026-03-06', open: 32.93, high: 34.2, low: 32.5, close: 33.5, volume: 1100 },
  { date: '2026-03-09', open: 33.5, high: 34, low: 32.8, close: 33.1, volume: 900 },
]

/** 图表主数据响应（带快照摘要）。`ok=false` 用来模拟后端没给摘要。 */
function klinesResponse(
  symbol: string,
  opts: { digest?: string | null; bars?: Bar[]; instrument?: string; snapshot?: boolean } = {},
) {
  const bars = opts.bars ?? BARS
  const digest = opts.digest === undefined ? DIGEST_A : opts.digest
  const withSnapshot = opts.snapshot !== false
  return {
    symbol,
    market: 'CN',
    interval: '1d',
    is_index: opts.instrument === 'index',
    klines: bars,
    source: 'pg',
    ...(withSnapshot
      ? {
          chan_snapshot: {
            schema: 'sida-chan-snapshot-v1',
            rules_version: 'chan-fractal-v1',
            instrument: opts.instrument ?? 'stock',
            interval: '1d',
            bars: bars.length,
            as_of_day: bars.length ? bars[bars.length - 1].date : null,
            digest,
            ...(digest === null ? { unavailable_reason: 'bars_not_digestible' } : {}),
          },
        }
      : {}),
  }
}

/** 结构端点响应：默认 1 顶(confirmed) + 1 底(candidate) + 1 blocked。 */
function structureResponse(over: Record<string, unknown> = {}) {
  return {
    symbol: '600519',
    market: 'CN',
    instrument: 'stock',
    interval: '1d',
    rules_version: 'chan-fractal-v1',
    snapshot_digest: DIGEST_A,
    as_of_day: '2026-03-06',
    omitted_latest_reason: 'current_day_completion_unverified',
    source: 'pg',
    adjustment: 'unknown',
    finality: 'unverified',
    fractals: [
      { kind: 'top', status: 'confirmed', position_day: '2026-03-05', position_price: 33, confirmed_on: '2026-03-06' },
      { kind: 'bottom', status: 'candidate', position_day: '2026-03-09', position_price: 32.8, confirmed_on: null },
      // blocked: 只允许"不画", 不许当成结构
      { kind: 'top', status: 'blocked', position_day: '2026-03-06', position_price: 34.2, confirmed_on: null },
    ],
    not_implemented: ['pen', 'segment', 'centre', 'buy_sell_point'],
    disclaimer: '仅合并K线的三根分型; 不含笔/线段/中枢/买卖点, 不构成交易建议',
    ...over,
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

// ── 笔（pen）图层夹具（2026-11-17）──────────────────────────────────────
//
// 4 根 K 线 + 3 笔，端点**全部**落在真实存在的 K 线日上，三笔首尾相接（服务端笔链口径）：
//   ① up   confirmed  底 03-03 @29   → 顶 03-04 @33.2   (确认日 03-05)
//   ② down candidate  顶 03-04 @33.2 → 底 03-05 @31     (确认日必须 null)
//   ③ up   blocked    底 03-05 @31   → 顶 03-06 @32.5   (只计数, 一根线都不许有)
const PEN_BARS: Bar[] = [
  { date: '2026-03-03', open: 30, high: 30.4, low: 28.8, close: 30.2, volume: 800 },
  { date: '2026-03-04', open: 30.2, high: 33.4, low: 30, close: 33, volume: 900 },
  { date: '2026-03-05', open: 33, high: 33.4, low: 30.9, close: 31.2, volume: 1000 },
  { date: '2026-03-06', open: 31.2, high: 32.6, low: 31.1, close: 32.4, volume: 1100 },
]

const PEN_FRACTALS = [
  { kind: 'bottom', status: 'confirmed', position_day: '2026-03-03', position_price: 29, confirmed_on: '2026-03-04' },
  { kind: 'top', status: 'confirmed', position_day: '2026-03-04', position_price: 33.2, confirmed_on: '2026-03-05' },
  { kind: 'bottom', status: 'confirmed', position_day: '2026-03-05', position_price: 31, confirmed_on: '2026-03-06' },
]

const PENS_A = [
  {
    direction: 'up',
    status: 'confirmed',
    confirmed_on: '2026-03-05',
    kernel_version: 'chan-pen-v1',
    start: { kind: 'bottom', merged_index: 1, position_day: '2026-03-03', price: 29, status: 'confirmed', confirmed_on: '2026-03-04' },
    end: { kind: 'top', merged_index: 2, position_day: '2026-03-04', price: 33.2, status: 'confirmed', confirmed_on: '2026-03-05' },
  },
  {
    direction: 'down',
    status: 'candidate',
    confirmed_on: null,
    kernel_version: 'chan-pen-v1',
    start: { kind: 'top', merged_index: 2, position_day: '2026-03-04', price: 33.2, status: 'confirmed', confirmed_on: '2026-03-05' },
    end: { kind: 'bottom', merged_index: 3, position_day: '2026-03-05', price: 31, status: 'confirmed', confirmed_on: '2026-03-06' },
  },
  {
    direction: 'up',
    status: 'blocked',
    confirmed_on: null,
    kernel_version: 'chan-pen-v1',
    start: { kind: 'bottom', merged_index: 3, position_day: '2026-03-05', price: 31, status: 'confirmed', confirmed_on: '2026-03-06' },
    end: { kind: 'top', merged_index: 4, position_day: '2026-03-06', price: 32.5, status: 'candidate', confirmed_on: null },
  },
]

/** 另一只票（000001）的笔：端点整体平移，用来验证切股后旧笔线不会留在新图上。 */
const PENS_B = [
  {
    direction: 'down',
    status: 'confirmed',
    confirmed_on: '2026-03-05',
    kernel_version: 'chan-pen-v1',
    start: { kind: 'top', merged_index: 1, position_day: '2026-03-03', price: 30.4, status: 'confirmed', confirmed_on: '2026-03-04' },
    end: { kind: 'bottom', merged_index: 2, position_day: '2026-03-05', price: 30.9, status: 'confirmed', confirmed_on: '2026-03-06' },
  },
]

/** 带笔的结构响应（笔在**同一份**已验快照里，摘要与分型一致）。 */
const penStructure = (over: Record<string, unknown> = {}) =>
  structureResponse({
    as_of_day: '2026-03-06',
    fractals: PEN_FRACTALS,
    pens: PENS_A,
    pen_rules_version: 'chan-pen-v1',
    pen_counts: { total: 3, confirmed: 1, candidate: 1, blocked: 1 },
    ...over,
  })

/** 等笔真的落到图上（线到位后才断言样式/端点）。 */
async function waitForPens(n: number) {
  await waitFor(() => expect(penSeries()).toHaveLength(n), { timeout: 5000 })
}


/**
 * 等主图 K 线真的落图（状态条出现「N 根K线」）。
 *
 * 为什么必须等：`chanBinding`（摘要 + 完整性审计）是在 K 线响应**落地后**才成立的，
 * 早于它点击开关只会看到 `awaiting-bars` —— 那是真实状态，但不是用例要断言的那一段。
 */
async function waitForBars() {
  await waitFor(() => expect(screen.getByText(/根K线/)).toBeTruthy(), { timeout: 5000 })
}

/** 当前状态条的 phase（没有状态条 = 图层关/已就绪走了图例）。 */
const chanPhase = () => screen.queryByTestId('chan-status')?.getAttribute('data-chan-phase') ?? null

/** 图例上的 phase（就绪后状态条让位给图例，两者同源）。 */
const chanLegendPhase = () => screen.queryByTestId('chan-legend')?.getAttribute('data-chan-phase') ?? null

/**
 * 笔的 line series（图上还活着的）。
 *
 * 判据：Line 类型 + 明确设了 `lineStyle`（笔独有的样式开关；均线/趋势辅助线都不设它）
 * + 没被移除。这样断言直接对在"笔图层真的加了哪些线、还在不在"上。
 */
const penSeries = () =>
  H.series.filter(
    (s) => s.type === 'Line' && typeof s.options.lineStyle === 'number' && !s.removed,
  )

/** 均线/趋势辅助线（Line 类型但不设 lineStyle）—— 用来验证"笔清场不连带删别人的线"。 */
const maSeries = () =>
  H.series.filter((s) => s.type === 'Line' && typeof s.options.lineStyle !== 'number')

/** 'YYYY-MM-DD' → 图表日线时间戳（与组件 `toChartTime` 同口径：UTC 零点秒） */
const SEC = (day: string) => Date.parse(`${day}T00:00:00Z`) / 1000

beforeEach(() => {
  H.calls.length = 0
  H.opts.length = 0
  H.markers = []
  H.series.length = 0
  H.klines = (url: string) => Promise.resolve(klinesResponse(url.includes('000001') ? '000001' : '600519'))
  H.structure = () => Promise.resolve(structureResponse())
})

afterEach(() => {
  cleanup()
  H.klines = null
  H.structure = null
})

describe('KlineChart 缠论分型图层', () => {
  it('默认关: 不发 /chan-structure 请求, 图上没有缠论记号, 按钮可点但未激活', async () => {
    render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    await waitFor(() => expect(H.calls.some((u) => u.includes('/klines/600519?'))).toBe(true))
    await sleep(60)

    expect(structureCall()).toBeNull()
    expect(chanMarkers()).toHaveLength(0)

    const toggle = screen.getByTestId('chan-layer-toggle')
    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    expect((toggle as HTMLButtonElement).disabled).toBe(false)
  })

  it('打开开关: 用图表摘要请求结构, 只画 confirmed/candidate(blocked 不画), 且无买卖语义', async () => {
    render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    await waitFor(() => expect(screen.getByTestId('chan-layer-toggle').getAttribute('aria-pressed')).toBe('false'))

    fireEvent.click(screen.getByTestId('chan-layer-toggle'))
    await waitFor(() => expect(chanMarkers()).toHaveLength(2), { timeout: 5000 })

    const url = structureCall()
    expect(url).not.toBeNull()
    // 快照绑定 + 身份 + 口径 + 天数, 一个都不能少
    expect(url).toContain('/klines/600519/chan-structure')
    expect(url).toContain(`snapshot=${DIGEST_A}`)
    expect(url).toContain('instrument=stock')
    expect(url).toContain('interval=1d')
    expect(url).toContain('days=120')
    expect(url).toContain('market=CN')

    const ms = chanMarkers()
    const confirmed = ms.find((m) => m.chanStatus === 'confirmed')
    const candidate = ms.find((m) => m.chanStatus === 'candidate')
    expect(confirmed).toBeTruthy()
    expect(candidate).toBeTruthy()

    // 视觉可区分: 实心大号 vs 空心小号(○ 前缀) + 同色压淡
    expect(confirmed!.text).toBe('顶')
    expect(confirmed!.size).toBe(2)
    expect(confirmed!.color).toBe('#a855f7')
    expect(candidate!.text).toBe('○底')
    expect(candidate!.size).toBe(1)
    expect(String(candidate!.color)).toContain('rgba(')

    // 顶在上方 / 底在下方(几何语义), 形状不用箭头(避免读成买卖方向)
    expect(confirmed!.position).toBe('aboveBar')
    expect(candidate!.position).toBe('belowBar')
    expect(ms.every((m) => m.shape === 'circle')).toBe(true)

    // **无买卖语义**: 文字只有 顶/底(可带 ○)
    for (const m of ms) expect(['顶', '底', '○顶', '○底']).toContain(m.text)

    // blocked 那根(2026-03-06)绝不出现
    expect(ms.map((m) => m.chanDay)).not.toContain('2026-03-06')
  })

  it('图例透出诚实边界: 当日K排除 / 复权未知 / 终局未验证 / blocked 不绘为结构', async () => {
    render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    fireEvent.click(screen.getByTestId('chan-layer-toggle'))
    const legend = await screen.findByTestId('chan-legend', {}, { timeout: 5000 })

    expect(legend.getAttribute('data-chan-phase')).toBe('ready')
    expect(legend.textContent).toContain('已确认 1')
    expect(legend.textContent).toContain('待确认 1')
    expect(legend.textContent).toContain('blocked 1 个')
    expect(legend.textContent).toContain('当日K未参与计算')
    expect(legend.textContent).toContain('current_day_completion_unverified')
    expect(legend.textContent).toContain('复权口径 unknown')
    expect(legend.textContent).toContain('终局性 unverified')
    // 未实现的结构层要如实点名(有了线段/操作口径中枢之后, 仍不许把严格中枢/买卖点说成已提供)
    expect(legend.textContent).toContain('严格中枢(第18课定义)/买卖点仍未实现')
    // 本响应不含 segments/operational_centre → 图例如实说"未提供", 不假装"没有线段/没有中枢"
    expect(legend.textContent).toContain('线段未提供')
    expect(legend.textContent).toContain('中枢未提供')
    // 这份响应里没有 pens 字段 → 图例如实说"笔未提供", 不假装"没有笔"
    expect(legend.textContent).toContain('笔未提供')
    // 非卡片(与形态/情景图例同风格的一行式小字)
    expect(legend.className).not.toMatch(/rounded-lg border|card-surface/)
  })

  it('关掉开关: 缠论记号消失, 其它图层的记号(buy)不被连带删除', async () => {
    const { rerender } = render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        gsSignals={[{ date: '2026-03-05', side: 'G', confirmed: true, price: 32.93 }]}
      />,
    )
    fireEvent.click(screen.getByTestId('chan-layer-toggle'))
    await waitFor(() => expect(chanMarkers()).toHaveLength(2), { timeout: 5000 })
    const gsBefore = (H.markers as Marker[]).filter((m) => m.text === 'G')
    expect(gsBefore).toHaveLength(1) // 同一个 plugin 里两套记号共存

    fireEvent.click(screen.getByTestId('chan-layer-toggle'))
    await waitFor(() => expect(chanMarkers()).toHaveLength(0), { timeout: 5000 })
    // 缠论关掉 ≠ 把别人的标记一起清掉(单一 plugin 的 setMarkers 是整批替换, 必须重建全量)
    expect((H.markers as Marker[]).filter((m) => m.text === 'G')).toHaveLength(1)
    expect(screen.queryByTestId('chan-legend')).toBeNull()
    rerender(<KlineChart symbol="600519" market="CN" instrument="stock" />)
  })

  it('切股: 旧结构立即清掉, 请求换成新图的摘要, 迟到的旧响应不会画到新图上', async () => {
    // A(600519) 的结构慢 400ms 且分型日与 B 不同; B(000001) 立即返回
    H.klines = (url: string) => {
      const symbol = url.match(/\/klines\/([^?]+)\?/)?.[1] ?? '600519'
      return Promise.resolve(
        klinesResponse(symbol, { digest: symbol === '000001' ? DIGEST_B : DIGEST_A }),
      )
    }
    H.structure = (url: string) => {
      const isB = url.includes('snapshot=' + DIGEST_B)
      const payload = isB
        ? structureResponse({
            symbol: '000001',
            snapshot_digest: DIGEST_B,
            fractals: [
              { kind: 'bottom', status: 'confirmed', position_day: '2026-03-09', position_price: 32.8, confirmed_on: '2026-03-10' },
            ],
          })
        : structureResponse({
            symbol: '600519',
            snapshot_digest: DIGEST_A,
            fractals: [
              { kind: 'top', status: 'confirmed', position_day: '2026-03-05', position_price: 33, confirmed_on: '2026-03-06' },
            ],
          })
      return isB ? Promise.resolve(payload) : sleep(400).then(() => payload)
    }

    const { rerender } = render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    fireEvent.click(screen.getByTestId('chan-layer-toggle'))
    // A 的结构请求已发出但还没回来
    await waitFor(() => expect(structureCall()).toContain(`snapshot=${DIGEST_A}`))

    rerender(<KlineChart symbol="000001" market="CN" instrument="stock" />)
    await waitFor(
      () => {
        const days = chanMarkers().map((m) => m.chanDay)
        expect(days).toContain('2026-03-09')
      },
      { timeout: 5000 },
    )
    // B 的请求用的是 B 的摘要, 不是 A 的
    const urls = H.calls.filter((u) => u.includes('/chan-structure'))
    expect(urls.some((u) => u.includes(`snapshot=${DIGEST_B}`))).toBe(true)

    // 等 A 的迟到响应落地: 它必须被丢弃(否则图上会多出 A 的 2026-03-05 顶)
    await sleep(500)
    const daysAfter = chanMarkers().map((m) => m.chanDay)
    expect(daysAfter).toEqual(['2026-03-09'])
    expect(screen.getByTestId('chan-legend').getAttribute('data-chan-phase')).toBe('ready')
  })

  it('409 快照不一致: 拒绝绘制并在图上说明(不静默、不拿旧结构凑)', async () => {
    H.structure = () => {
      const err = new Error('图表快照已变化(内容摘要不匹配), 请刷新图表') as Error & { status: number }
      err.status = 409
      return Promise.reject(err)
    }
    render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    await waitForBars()
    fireEvent.click(screen.getByTestId('chan-layer-toggle'))

    await waitFor(() => expect(chanPhase()).toBe('snapshot-mismatch'), { timeout: 5000 })
    const status = screen.getByTestId('chan-status')
    expect(status.textContent).toContain('409')
    expect(status.textContent).toContain('已拒绝绘制')
    expect(chanMarkers()).toHaveLength(0)
    expect(screen.queryByTestId('chan-legend')).toBeNull()
  })

  it('结构回传摘要与本次请求不一致 → 同样拒绝绘制(摘要必须与当前画面一致)', async () => {
    // 服务端"答非所问": 回的是另一份快照的结构
    H.structure = () => Promise.resolve(structureResponse({ snapshot_digest: DIGEST_B }))
    render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    await waitForBars()
    fireEvent.click(screen.getByTestId('chan-layer-toggle'))

    await waitFor(() => expect(chanPhase()).toBe('snapshot-mismatch'), { timeout: 5000 })
    const status = screen.getByTestId('chan-status')
    expect(status.textContent).toContain('快照摘要与本次请求不一致')
    expect(chanMarkers()).toHaveLength(0)
  })

  it('图表响应没有快照摘要: 不请求结构, 并说明"无法绑定"', async () => {
    H.klines = () => Promise.resolve(klinesResponse('600519', { digest: null }))
    render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    await waitForBars()
    fireEvent.click(screen.getByTestId('chan-layer-toggle'))

    await waitFor(() => expect(chanPhase()).toBe('no-snapshot'), { timeout: 5000 })
    const status = screen.getByTestId('chan-status')
    expect(status.textContent).toContain('bars_not_digestible')
    expect(structureCall()).toBeNull()
    expect(chanMarkers()).toHaveLength(0)
  })

  it('原 K 线含重复交易日(前端会去重) → 拒绝结构并给出原因码, 不发结构请求', async () => {
    H.klines = () =>
      Promise.resolve(
        klinesResponse('600519', {
          bars: [BARS[0], BARS[1], { ...BARS[1] }, BARS[2]], // 同日重复
        }),
      )
    render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    await waitForBars()
    fireEvent.click(screen.getByTestId('chan-layer-toggle'))

    await waitFor(() => expect(chanPhase()).toBe('bars-unreliable'), { timeout: 5000 })
    const status = screen.getByTestId('chan-status')
    expect(status.textContent).toContain('重复交易日')
    expect(status.textContent).toContain('duplicate-days')
    expect(structureCall()).toBeNull()
    expect(chanMarkers()).toHaveLength(0)
  })

  it('身份不可靠(未传 instrument): 按钮禁用、不发请求; 父组件强开时如实说明禁用原因', async () => {
    render(<KlineChart symbol="600519" market="CN" />)
    await waitFor(() => expect(H.calls.some((u) => u.includes('/klines/600519?'))).toBe(true))
    const toggle = screen.getByTestId('chan-layer-toggle') as HTMLButtonElement
    expect(toggle.disabled).toBe(true)
    fireEvent.click(toggle)
    await sleep(60)
    expect(structureCall()).toBeNull()
    expect(chanMarkers()).toHaveLength(0)

    cleanup()
    H.calls.length = 0
    render(<KlineChart symbol="600519" market="CN" layersVisible={{ chan: true }} />)
    const status = await screen.findByTestId('chan-status', {}, { timeout: 5000 })
    expect(status.getAttribute('data-chan-phase')).toBe('identity-unknown')
    expect(status.textContent).toContain('stock/index')
    expect(structureCall()).toBeNull()
  })

  it('非日K周期: 不发请求、不用日线分型贴周/月柱(文案说明只提供 1d)', async () => {
    render(<KlineChart symbol="600519" market="CN" instrument="stock" layersVisible={{ chan: true }} />)
    await waitFor(() => expect(chanMarkers().length).toBeGreaterThan(0), { timeout: 5000 })

    fireEvent.click(screen.getByText('周K'))
    await waitFor(
      () => expect(screen.getByTestId('chan-status').getAttribute('data-chan-phase')).toBe('not-daily'),
      { timeout: 5000 },
    )
    expect(chanMarkers()).toHaveLength(0)
    expect(H.calls.filter((u) => u.includes('/chan-structure'))).toHaveLength(1) // 只有日K那一次
  })

  // ── 复审 2026-11-17: 切股串摘要 / 超时挂住 / 409 无恢复动作 / 非法日期 / 加载失败装等待 ──

  it('切股: 新标的 K 线未落地前, 绝不拿旧摘要去请求新标的的结构(绑定必须带 symbol/market + 渲染代次)', async () => {
    H.klines = (url: string) => {
      const symbol = url.match(/\/klines\/([^?]+)\?/)?.[1] ?? '600519'
      // B 的 K 线慢 250ms —— 这段窗口正是旧代码用 A 的摘要请求 B 结构的时刻
      if (symbol === '000001') {
        return sleep(250).then(() => klinesResponse('000001', { digest: DIGEST_B }))
      }
      return Promise.resolve(klinesResponse('600519'))
    }
    H.structure = (url: string) =>
      url.includes(`snapshot=${DIGEST_B}`)
        ? Promise.resolve(structureResponse({ symbol: '000001', snapshot_digest: DIGEST_B }))
        : sleep(80).then(() => structureResponse())

    const { rerender } = render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    fireEvent.click(screen.getByTestId('chan-layer-toggle'))
    await waitFor(() => expect(structureCall()).toContain(`snapshot=${DIGEST_A}`))
    const before = H.calls.filter((u) => u.includes('/chan-structure')).length

    rerender(<KlineChart symbol="000001" market="CN" instrument="stock" />)
    await sleep(120) // B 的 K 线还没回来
    const during = H.calls.filter((u) => u.includes('/chan-structure'))
    expect(during).toHaveLength(before) // 一个多余的请求都不许有
    expect(during.some((u) => u.includes('/klines/000001/chan-structure'))).toBe(false)

    // B 的 K 线落地后才允许请求 B 的结构, 且必须用 B 的摘要
    await waitFor(
      () =>
        expect(
          H.calls.some(
            (u) => u.includes('/klines/000001/chan-structure') && u.includes(`snapshot=${DIGEST_B}`),
          ),
        ).toBe(true),
      { timeout: 5000 },
    )
    expect(
      H.calls.filter(
        (u) => u.includes('/klines/000001/chan-structure') && u.includes(`snapshot=${DIGEST_A}`),
      ),
    ).toEqual([])
  })

  it('结构请求悬挂(响应体卡住): 超时后显式失败并可重读图表重试, 不会永远停在"加载中"', async () => {
    let calls = 0
    H.structure = () => {
      calls += 1
      // 第一次: 永远不 settle, 也不理 abort —— 最坏形态(响应体悬挂)
      return calls === 1 ? new Promise<never>(() => {}) : Promise.resolve(structureResponse())
    }
    render(<KlineChart symbol="600519" market="CN" instrument="stock" layersVisible={{ chan: true }} />)
    await waitForBars()

    // 超时(120ms)必须自己落地: 不能依赖 fetchAPI 的 timeoutMs(传了 signal 时它不建计时器)
    await waitFor(() => expect(chanPhase()).toBe('error'), { timeout: 5000 })
    expect(screen.getByTestId('chan-status').textContent).toContain('超时')
    expect(chanMarkers()).toHaveLength(0)

    fireEvent.click(screen.getByTestId('chan-retry'))
    await waitFor(() => expect(chanLegendPhase()).toBe('ready'), { timeout: 5000 })
    expect(calls).toBe(2)
    expect(chanMarkers()).toHaveLength(2)
  })

  it('409: 界面上给出"重读图表"动作, 点击才重取主数据并跟随新摘要再请求(不自动无限重试)', async () => {
    let klinesCalls = 0
    H.klines = () => {
      klinesCalls += 1
      return Promise.resolve(klinesResponse('600519'))
    }
    let structureCalls = 0
    H.structure = () => {
      structureCalls += 1
      if (structureCalls === 1) {
        const err = new Error('图表快照已变化(内容摘要不匹配), 请刷新图表') as Error & { status: number }
        err.status = 409
        return Promise.reject(err)
      }
      return Promise.resolve(structureResponse())
    }

    render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    await waitForBars()
    fireEvent.click(screen.getByTestId('chan-layer-toggle'))
    await waitFor(() => expect(chanPhase()).toBe('snapshot-mismatch'), { timeout: 5000 })

    // 不许自动重试(409 是诚实结果, 自动循环只会把"图变了"变成静默 SPA 打转)
    await sleep(200)
    expect(structureCalls).toBe(1)
    expect(klinesCalls).toBe(1)

    fireEvent.click(screen.getByTestId('chan-retry'))
    await waitFor(() => expect(chanLegendPhase()).toBe('ready'), { timeout: 5000 })
    expect(klinesCalls).toBe(2) // 真的重读了图表主数据
    const reload = H.opts.filter((o) => o.url.includes('/klines/600519?')).pop()
    expect(reload?.options?.cacheMode).toBe('reload') // 且绕过 30s 响应缓存
    const urls = H.calls.filter((u) => u.includes('/chan-structure'))
    expect(urls).toHaveLength(2)
    expect(urls[1]).toContain(`snapshot=${DIGEST_A}`) // 跟着重新取到的摘要再请求
    expect(chanMarkers()).toHaveLength(2)
  })

  it('非法日历日期(2026-02-30)不归一到 03-02: 分型脏行丢弃, 绝不画到别的柱子上', async () => {
    H.klines = () =>
      Promise.resolve(
        klinesResponse('600519', {
          bars: [
            { date: '2026-02-27', open: 31, high: 32, low: 30.5, close: 31.5, volume: 800 },
            { date: '2026-03-02', open: 31.5, high: 33, low: 31.2, close: 32.6, volume: 900 },
            { date: '2026-03-05', open: 32.6, high: 33.4, low: 32.2, close: 32.93, volume: 1000 },
          ],
        }),
      )
    H.structure = () =>
      Promise.resolve(
        structureResponse({
          fractals: [
            // 日历上不存在的一天: V8 会把它解析成 2026-03-02 —— 正好是图上真有一根 K 线的日子
            { kind: 'bottom', status: 'confirmed', position_day: '2026-02-30', position_price: 32.8, confirmed_on: '2026-03-02' },
            { kind: 'top', status: 'confirmed', position_day: '2026-03-05', position_price: 33, confirmed_on: '2026-03-06' },
          ],
        }),
      )

    render(<KlineChart symbol="600519" market="CN" instrument="stock" layersVisible={{ chan: true }} />)
    await waitFor(() => expect(chanLegendPhase()).toBe('ready'), { timeout: 5000 })

    const legend = screen.getByTestId('chan-legend')
    expect(legend.textContent).toContain('脏行 1 个已丢弃')
    expect(chanMarkers().map((m) => m.chanDay)).toEqual(['2026-03-05'])
    expect(chanMarkers().some((m) => m.chanDay === '2026-03-02')).toBe(false)
  })

  it('K 线加载失败: 图层状态明确失败(不是"等待K线就绪"的假等待), 也不发结构请求', async () => {
    H.klines = () => Promise.reject(new Error('upstream 502'))
    render(<KlineChart symbol="600519" market="CN" instrument="stock" layersVisible={{ chan: true }} />)

    await waitFor(() => expect(chanPhase()).toBe('bars-failed'), { timeout: 5000 })
    const status = screen.getByTestId('chan-status')
    expect(status.textContent).toContain('加载失败')
    expect(status.textContent).toContain('upstream 502')
    expect(status.textContent).not.toContain('等待当日 K 线')
    expect(structureCall()).toBeNull()
    expect(chanMarkers()).toHaveLength(0)
  })

  it('重试途中关图层: 在途请求被取消, 迟到结果不写状态也不留旧记号; 再打开用当前绑定重来', async () => {
    let calls = 0
    H.structure = () => {
      calls += 1
      if (calls === 1) {
        const err = new Error('图表快照已变化(内容摘要不匹配), 请刷新图表') as Error & { status: number }
        err.status = 409
        return Promise.reject(err)
      }
      // 60ms < 测试注入的 120ms 超时: 它是"在途", 不是"超时"
      return sleep(60).then(() => structureResponse())
    }
    render(<KlineChart symbol="600519" market="CN" instrument="stock" />)
    await waitForBars()
    const toggle = screen.getByTestId('chan-layer-toggle')
    fireEvent.click(toggle)
    await waitFor(() => expect(chanPhase()).toBe('snapshot-mismatch'), { timeout: 5000 })

    fireEvent.click(screen.getByTestId('chan-retry'))
    await waitFor(() => expect(calls).toBe(2), { timeout: 5000 })
    // 第二个请求还在路上 → 关图层
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.queryByTestId('chan-status')).toBeNull(), { timeout: 5000 })
    expect(chanMarkers()).toHaveLength(0)

    await sleep(200) // 迟到响应落地
    expect(screen.queryByTestId('chan-status')).toBeNull()
    expect(screen.queryByTestId('chan-legend')).toBeNull()
    expect(chanMarkers()).toHaveLength(0)

    // 再打开: 用当前绑定重新请求(不被那条迟到响应顶替)
    fireEvent.click(toggle)
    await waitFor(() => expect(chanLegendPhase()).toBe('ready'), { timeout: 5000 })
    expect(calls).toBe(3)
    expect(chanMarkers()).toHaveLength(2)
  })
})

// ── 纯函数口径（与服务端契约逐条对齐；不渲染组件） ──────────────────────
describe('chan-layer 纯函数', () => {
  it('blocked 只计数不绘制; 脏行丢弃并计数; 可画分型按发生日升序', async () => {
    const { normalizeChanStructure, buildChanMarkers } = await import('@sida/biz-ui/lib/chan-layer')
    const s = normalizeChanStructure({
      snapshot_digest: DIGEST_A,
      fractals: [
        { kind: 'bottom', status: 'confirmed', position_day: '2026-03-09', position_price: 32.8, confirmed_on: '2026-03-10' },
        { kind: 'top', status: 'confirmed', position_day: '2026-03-05', position_price: 33, confirmed_on: '2026-03-06' },
        { kind: 'top', status: 'blocked', position_day: '2026-03-06', position_price: 34.2, confirmed_on: null },
        // 脏行: kind 不认识 / 价格非正 / 日期不是 YYYY-MM-DD
        { kind: 'peak', status: 'confirmed', position_day: '2026-03-04', position_price: 33, confirmed_on: null },
        { kind: 'top', status: 'candidate', position_day: '2026-03-03', position_price: 0, confirmed_on: null },
        { kind: 'top', status: 'candidate', position_day: '3/3/2026', position_price: 30, confirmed_on: null },
      ],
    })
    expect(s).not.toBeNull()
    expect(s!.blocked).toBe(1)
    expect(s!.dropped).toBe(3)
    expect(s!.fractals.map((f) => `${f.kind}@${f.positionDay}`)).toEqual(['top@2026-03-05', 'bottom@2026-03-09'])
    // 缺失的复权/终局字段按后端同口径默认, 不写成"已复权/已终局"
    expect(s!.adjustment).toBe('unknown')
    expect(s!.finality).toBe('unverified')

    const markers = buildChanMarkers(s!, { top: '#a855f7', bottom: '#0ea5e9' })
    expect(markers).toHaveLength(2)
    expect(markers.map((m) => m.text)).toEqual(['顶', '底'])
    expect(markers.map((m) => m.position)).toEqual(['aboveBar', 'belowBar'])
    // 文本里绝不出现买卖/B/S 字样
    for (const m of markers) expect(m.text).not.toMatch(/[买卖BS]/)
  })

  it('摘要必须是完整 64 位小写十六进制; 缺失则整份结构不可校验(返回 null)', async () => {
    const { normalizeChanStructure, isChanDigest } = await import('@sida/biz-ui/lib/chan-layer')
    expect(isChanDigest(DIGEST_A)).toBe(true)
    expect(isChanDigest(DIGEST_A.toUpperCase())).toBe(false) // 大写不"修正"
    expect(isChanDigest(DIGEST_A.slice(0, 32))).toBe(false) // 截短不"补齐"
    expect(normalizeChanStructure({ fractals: [] })).toBeNull()
    expect(normalizeChanStructure({ snapshot_digest: DIGEST_A })).toBeNull() // 缺 fractals ≠ 没有分型
    expect(normalizeChanStructure({ snapshot_digest: DIGEST_A, fractals: [] })).toMatchObject({
      fractals: [],
      blocked: 0,
    })
  })

  it('K线完整性审计: 重复日/非升序/OHLC非法 一律拒绝(前端渲染链会改写这些行)', async () => {
    const { auditBarsTransform } = await import('@sida/biz-ui/lib/chan-layer')
    const clean = [
      { date: '2026-03-05', open: 1, high: 2, low: 0.5, close: 1.5 },
      { date: '2026-03-06', open: 1.5, high: 2, low: 1, close: 1.8 },
    ]
    expect(auditBarsTransform(clean).ok).toBe(true)
    expect(auditBarsTransform([]).issue).toBe('no-bars')
    expect(auditBarsTransform([clean[0], clean[0]]).issue).toBe('duplicate-days')
    expect(auditBarsTransform([clean[1], clean[0]]).issue).toBe('unsorted-days')
    expect(auditBarsTransform([clean[0], { ...clean[1], close: null }]).issue).toBe('invalid-ohlc')
  })

  it('严格日历日: 2026-02-30/13月/4月31日 一律非法(不归一化); 确认日非法或倒挂 → 该分型丢弃', async () => {
    const { isValidCalendarDay, normalizeChanStructure } = await import('@sida/biz-ui/lib/chan-layer')
    expect(isValidCalendarDay('2024-02-29')).toBe(true) // 真闰日
    expect(isValidCalendarDay('2026-02-29')).toBe(false) // 非闰年
    expect(isValidCalendarDay('2026-02-30')).toBe(false) // 正则合法、日历非法
    expect(isValidCalendarDay('2026-04-31')).toBe(false)
    expect(isValidCalendarDay('2026-13-01')).toBe(false)
    expect(isValidCalendarDay('2026-00-10')).toBe(false)
    expect(isValidCalendarDay('3/3/2026')).toBe(false)

    const s = normalizeChanStructure({
      snapshot_digest: DIGEST_A,
      fractals: [
        // 确认日 2026-02-30 在日历上不存在 → 这条"已确认"无法自证, 丢弃
        { kind: 'top', status: 'confirmed', position_day: '2026-03-05', position_price: 33, confirmed_on: '2026-02-30' },
        // 确认日早于分型日 → 倒挂, 丢弃
        { kind: 'bottom', status: 'confirmed', position_day: '2026-03-09', position_price: 32.8, confirmed_on: '2026-03-02' },
      ],
    })
    expect(s).not.toBeNull()
    expect(s!.dropped).toBe(2)
    expect(s!.fractals).toHaveLength(0)
  })

  it('请求控制器: 上游悬挂(响应体卡住)也会在超时后 settle; cancel 立刻 settle 为 aborted', async () => {
    const { startChanRequest } = await import('@sida/biz-ui/lib/chan-layer')
    // 上游永远不 settle, 也不理 abort —— 最坏形态
    const hung = startChanRequest({ timeoutMs: 30, request: () => new Promise<never>(() => {}) })
    const timedOut = await hung.result
    expect(timedOut.state).toBe('timeout')
    expect(hung.timedOut()).toBe(true)

    let aborted = false
    const cancelled = startChanRequest({
      timeoutMs: 5000,
      request: (sig) =>
        new Promise((_resolve, reject) => {
          sig.addEventListener('abort', () => {
            aborted = true
            reject(new DOMException('aborted', 'AbortError'))
          })
        }),
    })
    cancelled.cancel()
    expect((await cancelled.result).state).toBe('aborted')
    expect(aborted).toBe(true)

    const holder: { signal: AbortSignal | null } = { signal: null }
    const ok = startChanRequest({
      timeoutMs: 5000,
      request: (sig) => {
        holder.signal = sig
        return Promise.resolve(42)
      },
    })
    expect(await ok.result).toEqual({ state: 'ok', payload: 42 })
    expect(holder.signal?.aborted).toBe(false) // 成功后不误伤
  })

  it('快照自述身份与当前图不一致 → 拒绝(同码个股/指数不得互证)', async () => {
    const { pickChanSnapshot } = await import('@sida/biz-ui/lib/chan-layer')
    const payload = {
      chan_snapshot: { instrument: 'stock', interval: '1d', digest: DIGEST_A },
    }
    expect(pickChanSnapshot(payload, { instrument: 'stock', interval: '1d' }).ok).toBe(true)
    const wrong = pickChanSnapshot(payload, { instrument: 'index', interval: '1d' })
    expect(wrong.ok).toBe(false)
    expect(wrong.problem).toBe('identity-mismatch')
    const missing = pickChanSnapshot({ klines: [] }, { instrument: 'stock', interval: '1d' })
    expect(missing.problem).toBe('missing')
  })
})

// ── 笔（pen）图层：真组件 + 真 series 记录（2026-11-17） ─────────────────
//
// 断言对象是"组件真的往图表里加/删了什么"：每一笔一条 Lightweight Charts 原生
// LineSeries（两个点=服务端端点），已确认=细实线、待确认=细虚线、blocked 不画。
describe('KlineChart 缠论笔图层', () => {
  /** 打开图层 + 带笔的结构（挂 PEN_BARS 的 4 根 K 线）。 */
  function mountWithPens(extra: Record<string, unknown> = {}) {
    H.klines = () => Promise.resolve(klinesResponse('600519', { bars: PEN_BARS }))
    H.structure = () => Promise.resolve(penStructure(extra))
    return render(<KlineChart symbol="600519" market="CN" instrument="stock" layersVisible={{ chan: true }} />)
  }

  it('只画 confirmed/candidate 笔: 已确认细实线、待确认细虚线, 端点逐点等于服务端端点(不插值/不跨日造点)', async () => {
    mountWithPens()
    await waitForPens(2)

    const [confirmed, candidate] = penSeries()
    // 已确认: 实线(Solid=0) 细线(width 1)
    expect(confirmed.options.lineStyle).toBe(0)
    expect(confirmed.options.lineWidth).toBe(1)
    expect(confirmed.data).toEqual([
      { time: SEC('2026-03-03'), value: 29 },
      { time: SEC('2026-03-04'), value: 33.2 },
    ])
    // 待确认: 虚线(Dashed=2) + 同色压淡(rgba), 两个点同样是服务端端点
    expect(candidate.options.lineStyle).toBe(2)
    expect(String(candidate.options.color)).toContain('rgba(')
    expect(candidate.data).toEqual([
      { time: SEC('2026-03-04'), value: 33.2 },
      { time: SEC('2026-03-05'), value: 31 },
    ])

    // blocked 笔(底 03-05 → 顶 03-06): 一根线都不许有 —— 不是"浅色画一下"
    expect(penSeries()).toHaveLength(2)
    for (const s of penSeries()) {
      const days = (s.data as Array<{ time: number }>).map((p) => p.time)
      expect(days).not.toContain(SEC('2026-03-06'))
      expect(days).toHaveLength(2) // 每条线只有两个端点, 没有为了"连起来"补中间日
    }
    // 无买卖语义: 线本身不带任何文案/标签(只有几何端点与线型)
    for (const s of penSeries()) {
      expect(s.options.title).toBeUndefined()
      expect(s.options.text).toBeUndefined()
    }
  })

  it('图例: 分型与笔分列, 实线=已确认/虚线=待确认, blocked 笔只计数不绘, 线段/中枢未提供时如实说明', async () => {
    mountWithPens()
    const legend = await screen.findByTestId('chan-legend', {}, { timeout: 5000 })
    expect(legend.getAttribute('data-chan-phase')).toBe('ready')

    expect(legend.textContent).toContain('分型')
    expect(legend.textContent).toContain('笔')
    expect(legend.textContent).toContain('已确认(实线) 1')
    expect(legend.textContent).toContain('待确认(虚线) 1')
    expect(legend.textContent).toContain('blocked 笔 1 笔（不绘）')
    // 诚实边界不能因为"多了笔"被挤掉
    expect(legend.textContent).toContain('复权口径 unknown')
    expect(legend.textContent).toContain('终局性 unverified')
    // 这份响应没有 segments/operational_centre: 必须说"未提供", 而不是"没有线段/没有中枢"
    expect(legend.textContent).toContain('线段未提供')
    expect(legend.textContent).toContain('中枢未提供')
    expect(legend.textContent).toContain('严格中枢(第18课定义)/买卖点仍未实现')
    expect(legend.textContent).not.toContain('笔未提供')
    // 图例仍是一行式小字, 不是卡片
    expect(legend.className).not.toMatch(/rounded-lg border|card-surface/)
  })

  it('关图层: 笔的 line series 全部移除, 均线与其它标记照旧(不连带删别人的 series)', async () => {
    H.klines = () => Promise.resolve(klinesResponse('600519', { bars: PEN_BARS }))
    H.structure = () => Promise.resolve(penStructure())
    const { rerender } = render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true }}
        gsSignals={[{ date: '2026-03-03', side: 'G', confirmed: true, price: 30.2 }]}
      />,
    )
    await waitForPens(2)
    const drawn = penSeries().slice()
    expect(maSeries().length).toBeGreaterThan(0)
    const maBefore = maSeries().length

    rerender(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: false }}
        gsSignals={[{ date: '2026-03-03', side: 'G', confirmed: true, price: 30.2 }]}
      />,
    )
    await waitFor(() => expect(penSeries()).toHaveLength(0), { timeout: 5000 })
    // 每一条旧笔线都被显式移除(不是"留在那儿但看不见")
    for (const s of drawn) expect(s.removed).toBe(true)
    // 均线一条都没被删
    expect(maSeries()).toHaveLength(maBefore)
    expect(maSeries().every((s) => !s.removed)).toBe(true)
    // 其它图层的 marker 还在(单一 plugin 整批替换, 不许被笔连带清空)
    expect((H.markers as Marker[]).filter((m) => m.text === 'G')).toHaveLength(1)
  })

  it('端点日不在当前画面(停牌/区间外): 这一笔不画, 图例如实说"端点日不在图上"', async () => {
    mountWithPens({
      as_of_day: '2026-03-10',
      // 终点在 03-10 —— 图上根本没有这根 K 线
      pens: [
        {
          direction: 'up',
          status: 'confirmed',
          confirmed_on: '2026-03-10',
          start: { kind: 'bottom', position_day: '2026-03-03', price: 29 },
          end: { kind: 'top', position_day: '2026-03-10', price: 34 },
        },
      ],
    })
    const legend = await screen.findByTestId('chan-legend', {}, { timeout: 5000 })
    await waitFor(() => expect(legend.textContent).toContain('端点日不在图上'), { timeout: 5000 })
    expect(penSeries()).toHaveLength(0)
    expect(legend.textContent).toContain('1 笔端点日不在图上，未绘制')
  })

  it('笔链不连续(疑似混了两个批次): 整批拒收、一根线都不画、并在图例给出原因码', async () => {
    mountWithPens({
      pens: [
        {
          direction: 'up',
          status: 'confirmed',
          confirmed_on: '2026-03-05',
          start: { kind: 'bottom', position_day: '2026-03-03', price: 29 },
          end: { kind: 'top', position_day: '2026-03-04', price: 33.2 },
        },
        {
          // 起点不是上一笔的终点(价格对不上) → 不是同一批数据算出来的笔链
          direction: 'down',
          status: 'confirmed',
          confirmed_on: '2026-03-06',
          start: { kind: 'top', position_day: '2026-03-04', price: 33.9 },
          end: { kind: 'bottom', position_day: '2026-03-05', price: 31 },
        },
      ],
    })
    const legend = await screen.findByTestId('chan-legend', {}, { timeout: 5000 })
    await waitFor(() => expect(legend.textContent).toContain('pen-chain-broken'), { timeout: 5000 })
    expect(penSeries()).toHaveLength(0)
    expect(legend.textContent).toContain('不连续')
  })

  it('切股: 旧图的笔线被移除, 新图只画新标的的笔; 迟到的旧响应不会把旧笔补回新图', async () => {
    H.klines = (url: string) => {
      const symbol = url.match(/\/klines\/([^?]+)\?/)?.[1] ?? '600519'
      return Promise.resolve(
        klinesResponse(symbol, { bars: PEN_BARS, digest: symbol === '000001' ? DIGEST_B : DIGEST_A }),
      )
    }
    H.structure = (url: string) =>
      url.includes(`snapshot=${DIGEST_B}`)
        ? Promise.resolve(
            penStructure({
              symbol: '000001',
              snapshot_digest: DIGEST_B,
              pens: PENS_B,
              pen_counts: { total: 1, confirmed: 1, candidate: 0, blocked: 0 },
            }),
          )
        : sleep(60).then(() => penStructure())

    const { rerender } = render(
      <KlineChart symbol="600519" market="CN" instrument="stock" layersVisible={{ chan: true }} />,
    )
    await waitForPens(2)
    const oldA = penSeries().slice()

    rerender(<KlineChart symbol="000001" market="CN" instrument="stock" layersVisible={{ chan: true }} />)
    await waitForPens(1)
    // 新图上的这一笔是 B 的端点(顶 03-03 @30.4 → 底 03-05 @30.9)
    expect(penSeries()[0].data).toEqual([
      { time: SEC('2026-03-03'), value: 30.4 },
      { time: SEC('2026-03-05'), value: 30.9 },
    ])
    for (const s of oldA) expect(s.removed).toBe(true)

    // A 的迟到响应落地: 不许补回 A 的笔(否则新图上同时画着两只票的笔)
    await sleep(200)
    expect(penSeries()).toHaveLength(1)
    expect(penSeries()[0].data).toEqual([
      { time: SEC('2026-03-03'), value: 30.4 },
      { time: SEC('2026-03-05'), value: 30.9 },
    ])
  })

  it('409 快照已变化: 之前画上的笔线全部撤掉, 不在新图上留旧结论', async () => {
    let structCalls = 0
    H.klines = () => Promise.resolve(klinesResponse('600519', { bars: PEN_BARS }))
    H.structure = () => {
      structCalls += 1
      if (structCalls === 1) return Promise.resolve(penStructure())
      const err = new Error('图表快照已变化(内容摘要不匹配), 请刷新图表') as Error & { status: number }
      err.status = 409
      return Promise.reject(err)
    }
    render(<KlineChart symbol="600519" market="CN" instrument="stock" layersVisible={{ chan: true }} />)
    await waitForPens(2)

    // 切到周K(笔不适用) → 再切回日K: 第二次结构请求 409
    fireEvent.click(screen.getByText('周K'))
    await waitFor(() => expect(chanPhase()).toBe('not-daily'), { timeout: 5000 })
    expect(penSeries()).toHaveLength(0)
    fireEvent.click(screen.getByText('日K'))
    await waitFor(() => expect(chanPhase()).toBe('snapshot-mismatch'), { timeout: 5000 })

    expect(penSeries()).toHaveLength(0)
    expect(screen.getByTestId('chan-status').textContent).toContain('409')
    // 旧笔线确实被移除(不是被"新一堆线盖住")
    expect(H.series.filter((s) => typeof s.options.lineStyle === 'number' && s.removed).length).toBe(2)
  })

  it('周期切到非日K: 笔线立即撤掉(日线笔不贴到周/月柱上)', async () => {
    mountWithPens()
    await waitForPens(2)
    fireEvent.click(screen.getByText('周K'))
    await waitFor(() => expect(penSeries()).toHaveLength(0), { timeout: 5000 })
    expect(chanPhase()).toBe('not-daily')
  })
})

// ── onChanSnapshotChange：把"当前画面那份可校验快照"交给外部(数智参谋) ─────
//
// 契约(逐条钉住, 消费方据此绑定自己的结构/笔查询):
//  - 只在 **同一张已落图的日K** + 摘要可校验 + 前端渲染链未改写原始 K 线时给非 null;
//  - 载荷身份全部来自**已核实的事实**(props.symbol / 响应自述 instrument / 请求的 days),
//    绝不做"看代码像指数就猜 index"这类推测;
//  - 切标的/周期/天数、K 线加载失败、重读开始、卸载 → 立刻 null;
//  - 值没变不重复回调(幂等), 不回消息、不弹提示。
describe('KlineChart onChanSnapshotChange', () => {
  const okKlines = (digest: string) => () =>
    Promise.resolve(klinesResponse('600519', { bars: PEN_BARS, digest }))

  it('落图且 audit.ok: 回调给出摘要与 exact 身份(symbol/market/instrument/interval/days)', async () => {
    H.klines = okKlines(DIGEST_A)
    H.structure = () => Promise.resolve(penStructure())
    const seen: Array<Record<string, unknown> | null> = []
    render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    await waitFor(() => expect(seen.some((s) => s && s.digest === DIGEST_A)).toBe(true), { timeout: 5000 })

    const signal = seen.filter((s) => s !== null).pop()!
    expect(signal).toEqual({
      symbol: '600519',
      market: 'CN',
      instrument: 'stock',
      interval: '1d',
      days: 120,
      digest: DIGEST_A,
    })
    // 挂载时先给一次 null(还没有可校验的图), 之后才是真信号 —— 不沉默、也不假装
    expect(seen[0]).toBeNull()
  })

  it('与缠论图层开关无关: 图层关着也照样给出快照(外部绑定不需要先打开图例)', async () => {
    H.klines = okKlines(DIGEST_A)
    H.structure = () => Promise.resolve(penStructure())
    const seen: Array<Record<string, unknown> | null> = []
    render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    await waitFor(() => expect(seen.filter(Boolean)).toHaveLength(1), { timeout: 5000 })
    expect(structureCall()).toBeNull() // 图层关 = 一个结构请求都不发
    expect(seen.filter(Boolean)[0]!.digest).toBe(DIGEST_A)
  })

  it('切股: 立刻回调 null(旧图的摘要不许挂在新标的上), 新图落图后再给新摘要', async () => {
    H.klines = (url: string) => {
      const symbol = url.match(/\/klines\/([^?]+)\?/)?.[1] ?? '600519'
      if (symbol === '000001') {
        return sleep(200).then(() => klinesResponse('000001', { bars: PEN_BARS, digest: DIGEST_B }))
      }
      return Promise.resolve(klinesResponse('600519', { bars: PEN_BARS, digest: DIGEST_A }))
    }
    H.structure = () => Promise.resolve(penStructure())
    const seen: Array<Record<string, unknown> | null> = []
    const { rerender } = render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    await waitFor(() => expect(seen.filter((s) => s && s.digest === DIGEST_A)).toHaveLength(1), { timeout: 5000 })

    rerender(
      <KlineChart
        symbol="000001"
        market="CN"
        instrument="stock"
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    // 新标的的 K 线还没回来 → 必须已经是 null(不许留 A 的摘要)
    await waitFor(() => expect(seen[seen.length - 1]).toBeNull(), { timeout: 2000 })
    expect(seen.filter((s) => s && s.symbol === '000001')).toHaveLength(0)

    await waitFor(() => expect(seen.filter((s) => s && s.symbol === '000001')).toHaveLength(1), { timeout: 5000 })
    expect(seen.filter((s) => s && s.symbol === '000001')[0]).toEqual({
      symbol: '000001',
      market: 'CN',
      instrument: 'stock',
      interval: '1d',
      days: 120,
      digest: DIGEST_B,
    })
  })

  it('K 线加载失败: 只有 null(不拿上一份/别人的摘要冒充当前图)', async () => {
    H.klines = () => Promise.reject(new Error('upstream 502'))
    const seen: Array<Record<string, unknown> | null> = []
    render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    await waitFor(() => expect(seen.length).toBeGreaterThan(0), { timeout: 5000 })
    await sleep(80)
    expect(seen.filter(Boolean)).toHaveLength(0)
    expect(seen[seen.length - 1]).toBeNull()
  })

  it('原始 K 线会被前端改写(重复日)/身份不可靠: 一律不给快照', async () => {
    H.klines = () =>
      Promise.resolve(klinesResponse('600519', { bars: [PEN_BARS[0], PEN_BARS[1], { ...PEN_BARS[1] }, PEN_BARS[3]] }))
    const seen: Array<Record<string, unknown> | null> = []
    render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    await waitForBars()
    await sleep(80)
    expect(seen.filter(Boolean)).toHaveLength(0)

    // 没有显式 instrument → 身份不可靠, 不能给出"看起来是 stock"的快照
    cleanup()
    seen.length = 0
    H.klines = () => Promise.resolve(klinesResponse('600519', { bars: PEN_BARS }))
    render(
      <KlineChart
        symbol="600519"
        market="CN"
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    await waitForBars()
    await sleep(80)
    expect(seen.filter(Boolean)).toHaveLength(0)
  })

  it('周期切换: 非日K 期间回调 null(横线期/日线的摘要不许挂到周K上), 切回日K 再给出', async () => {
    H.klines = okKlines(DIGEST_A)
    H.structure = () => Promise.resolve(penStructure())
    const seen: Array<Record<string, unknown> | null> = []
    render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    await waitFor(() => expect(seen.filter(Boolean)).toHaveLength(1), { timeout: 5000 })

    fireEvent.click(screen.getByText('周K'))
    await waitFor(() => expect(seen[seen.length - 1]).toBeNull(), { timeout: 5000 })
    // 等到周K 主数据也落地之后, 仍然只有 null(interval 不是 1d 就绝不广播)
    await sleep(80)
    expect(seen.filter(Boolean)).toHaveLength(1)
    expect(seen.some((s) => s && s.interval !== '1d')).toBe(false)

    fireEvent.click(screen.getByText('日K'))
    await waitFor(() => expect(seen.filter(Boolean)).toHaveLength(2), { timeout: 5000 })
    expect(seen.filter(Boolean)[1]).toMatchObject({ symbol: '600519', interval: '1d', digest: DIGEST_A })
  })

  it('卸载: 立刻回调 null(消费方据此解绑, 不留一个指向已销毁图表的摘要)', async () => {
    H.klines = okKlines(DIGEST_A)
    const seen: Array<Record<string, unknown> | null> = []
    const { unmount } = render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    await waitFor(() => expect(seen.filter(Boolean)).toHaveLength(1), { timeout: 5000 })
    unmount()
    expect(seen[seen.length - 1]).toBeNull()
  })

  it('重读图表开始 → null; 结构取数失败(503)本身不改快照, 但重读期间必须为 null', async () => {
    let structCalls = 0
    H.klines = okKlines(DIGEST_A)
    H.structure = () => {
      structCalls += 1
      if (structCalls === 1) {
        const err = new Error('后端暂不可用') as Error & { status: number }
        err.status = 503
        return Promise.reject(err)
      }
      return Promise.resolve(penStructure())
    }
    const seen: Array<Record<string, unknown> | null> = []
    render(
      <KlineChart
        symbol="600519"
        market="CN"
        instrument="stock"
        layersVisible={{ chan: true }}
        onChanSnapshotChange={(s) => seen.push(s as Record<string, unknown> | null)}
      />,
    )
    await waitFor(() => expect(chanPhase()).toBe('error'), { timeout: 5000 })
    // 503 是"结构没取到", 不是"图变了" → 快照仍然有效
    expect(seen.filter(Boolean)).toHaveLength(1)

    seen.length = 0
    fireEvent.click(screen.getByTestId('chan-retry'))
    // 重读一开始就把旧绑定作废
    await waitFor(() => expect(seen[0]).toBeNull(), { timeout: 3000 })
    // 新图落图后重新给出同一份摘要(内容没变, 摘要就该一样)
    await waitFor(() => expect(seen.filter((s) => s && s.digest === DIGEST_A).length).toBe(1), { timeout: 5000 })
  })
})

// ── 笔的纯函数口径（与服务端 pen 载荷逐条对齐） ──────────────────────────
describe('chan-layer 笔纯函数', () => {
  const s = (pens: unknown[] | undefined, over: Record<string, unknown> = {}) => ({
    snapshot_digest: DIGEST_A,
    as_of_day: '2026-03-06',
    fractals: [],
    pens,
    ...over,
  })
  const okPen = {
    direction: 'up',
    status: 'confirmed',
    confirmed_on: '2026-03-05',
    start: { kind: 'bottom', position_day: '2026-03-03', price: 29 },
    end: { kind: 'top', position_day: '2026-03-04', price: 33.2 },
  }

  it('blocked 笔只计数不绘制; 已确认/候选进入可画集合; 缺 pens 字段 = 未提供(不是"没有笔")', async () => {
    const { normalizeChanStructure } = await import('@sida/biz-ui/lib/chan-layer')
    const blocked = {
      direction: 'down',
      status: 'blocked',
      confirmed_on: null,
      start: { kind: 'top', position_day: '2026-03-04', price: 33.2 },
      end: { kind: 'bottom', position_day: '2026-03-05', price: 31 },
    }
    const candidate = {
      direction: 'down',
      status: 'candidate',
      confirmed_on: null,
      start: { kind: 'top', position_day: '2026-03-04', price: 33.2 },
      end: { kind: 'bottom', position_day: '2026-03-05', price: 31 },
    }
    const onlyOk = normalizeChanStructure(s([okPen, blocked]))
    expect(onlyOk!.pensProvided).toBe(true)
    expect(onlyOk!.pens).toHaveLength(1)
    expect(onlyOk!.penBlocked).toBe(1)
    expect(onlyOk!.penDropped).toBe(0)
    expect(onlyOk!.penRejectReason).toBeNull()

    const withCandidate = normalizeChanStructure(s([okPen, candidate]))
    expect(withCandidate!.pens.map((p) => p.status)).toEqual(['confirmed', 'candidate'])

    const notProvided = normalizeChanStructure(s(undefined))
    expect(notProvided!.pensProvided).toBe(false)
    expect(notProvided!.pens).toEqual([])
  })

  it('单笔硬前提逐条拒收: 确认日缺失/倒挂/越过 as_of、候选带确认日、方向与端点类型不符、日历非法、start>=end', async () => {
    const { normalizeChanStructure } = await import('@sida/biz-ui/lib/chan-layer')
    const cases: Array<[string, Record<string, unknown>]> = [
      ['已确认缺确认日', { ...okPen, confirmed_on: null }],
      ['确认日倒挂', { ...okPen, confirmed_on: '2026-03-01' }],
      ['确认日越过 as_of', { ...okPen, confirmed_on: '2026-03-09' }],
      ['候选带确认日', { ...okPen, status: 'candidate' }],
      ['方向与端点类型不符', { ...okPen, direction: 'down' }],
      ['日历非法', { ...okPen, start: { kind: 'bottom', position_day: '2026-02-30', price: 29 } }],
      ['负价', { ...okPen, end: { kind: 'top', position_day: '2026-03-04', price: -1 } }],
      ['起点不早于终点', { ...okPen, end: { kind: 'top', position_day: '2026-03-03', price: 33.2 } }],
      ['端点穿越 as_of', { ...okPen, end: { kind: 'top', position_day: '2026-03-09', price: 33.2 } }],
      ['status 不认识', { ...okPen, status: 'maybe' }],
      // blocked 带确认日 = 自相矛盾("不足以确认"却写着哪天确认的)
      ['blocked 带确认日', { ...okPen, status: 'blocked' }],
    ]
    for (const [label, pen] of cases) {
      const out = normalizeChanStructure(s([pen]))
      expect(out!.pens, label).toHaveLength(0)
      expect(out!.penDropped, label).toBe(1)
      expect(out!.penRejects.length, label).toBe(1)
    }
  })

  it('笔链断裂(不同批次的笔): 整批拒收而不是"能连就连" —— 一根都不画并给出原因码', async () => {
    const { normalizeChanStructure } = await import('@sida/biz-ui/lib/chan-layer')
    const next = {
      direction: 'down',
      status: 'confirmed',
      confirmed_on: '2026-03-06',
      start: { kind: 'top', position_day: '2026-03-04', price: 33.9 }, // ≠ 上一笔终点 33.2
      end: { kind: 'bottom', position_day: '2026-03-05', price: 31 },
    }
    const out = normalizeChanStructure(s([okPen, next]))
    expect(out!.pens).toEqual([])
    expect(out!.penRejectReason).toBe('pen-chain-broken')
    expect(out!.penRejects.some((r) => r.reason === 'pen-chain-broken')).toBe(true)
    // 服务了 2 笔、画了 0 笔: 两笔都要进"拒收"账, 不许有无法解释的差额
    expect(out!.penDropped).toBe(2)
  })

  it('笔 → 线段: 端点日都在图上才画; 已确认实线/候选虚线; 两个点就是服务端端点(不插值、不跨日造线)', async () => {
    const { normalizeChanStructure, buildChanPenSegments } = await import('@sida/biz-ui/lib/chan-layer')
    const candidate = {
      direction: 'down',
      status: 'candidate',
      confirmed_on: null,
      start: { kind: 'top', position_day: '2026-03-04', price: 33.2 },
      end: { kind: 'bottom', position_day: '2026-03-05', price: 31 },
    }
    const structure = normalizeChanStructure(s([okPen, candidate]))!
    const colors = { confirmed: '#6d28d9', candidate: 'rgba(109, 40, 217, 0.6)' }

    const full = buildChanPenSegments(structure, {
      barDays: new Set(['2026-03-03', '2026-03-04', '2026-03-05']),
      colors,
    })
    expect(full.segments).toHaveLength(2)
    expect(full.segments[0]).toMatchObject({
      startDay: '2026-03-03',
      startPrice: 29,
      endDay: '2026-03-04',
      endPrice: 33.2,
      dashed: false,
      lineWidth: 1,
      color: colors.confirmed,
    })
    expect(full.segments[1]).toMatchObject({ dashed: true, color: colors.candidate })
    expect(full.skipped).toEqual([])

    // 终点那天图上没有 K 线 → 这一笔整笔不画(不给端点找"最近的柱子")
    const partial = buildChanPenSegments(structure, {
      barDays: new Set(['2026-03-03']),
      colors,
    })
    expect(partial.segments).toHaveLength(0)
    expect(partial.skipped.map((x) => x.reason)).toEqual([
      'endpoint-day-not-on-chart',
      'endpoint-day-not-on-chart',
    ])
    // 每段的键可溯源(起点日>终点日), 不含任何买卖语义
    expect(full.segments[0].key).toBe('2026-03-03>2026-03-04')
  })
})
