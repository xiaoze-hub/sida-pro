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

/** 假后端的可编程面：每个用例自己装 `klines` / `structure` 两个 handler。 */
const H = vi.hoisted(() => ({
  calls: [] as string[],
  /** fetchAPI 收到的 (url, options) —— 用来断言"重读图表"真的绕过了缓存 */
  opts: [] as Array<{ url: string; options?: Record<string, unknown> }>,
  /** 图层最后一次真正收到的 marker 列表（createSeriesMarkers 初值 + 每次 setMarkers） */
  markers: [] as unknown[],
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
    createChart: () => unknown
    LineStyle: Record<string, number>
  }
  return {
    ...base,
    LineStyle: { ...base.LineStyle, Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3 },
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

beforeEach(() => {
  H.calls.length = 0
  H.opts.length = 0
  H.markers = []
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
    expect(legend.textContent).toContain('不含笔/线段/中枢/买卖点')
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
