// @vitest-environment jsdom
/**
 * 纯时间点标注 → **真实 K 线 marker** 的实证测试（2026-10-29）。
 *
 * 为什么必须用真组件：marker 画在 canvas 上、由 lightweight-charts 的
 * `createSeriesMarkers` 承载。拿 mock 组件自说自话证明不了"这条标注真的上到图上、
 * 落在哪一天、没有价位的行没有被编出一个价位"。这里用：
 *
 *  - 生产组件 `@sida/biz-ui/components/KlineChart`（只把 canvas 层换成仓库既有的替身，
 *    见 vite.config test.alias），替身把每次 `setMarkers` 的**实际数组**记下来；
 *  - 生产接线 `useWorkbenchAnnotations` + `annotationLink`（页面用的是同一个）；
 *  - 生产状态条 `AnnotationLinkBar`（"已上图 / 该日没有 K 线没上图" 由它播报）；
 *  - 网络只替 `fetchAPI`，其余每一行都是生产代码。
 *
 * 覆盖：
 *  ① 用户确认 ts-only 标注 → 精确回读 → 真 marker 落在该日，且**没有**价位线被编出来；
 *  ② 刷新重读不重复、不丢；
 *  ③ 该日没有 K 线（周末）→ 不画，状态条如实说"未上图"（不谎称已上图）；
 *  ④ 换标的立刻清掉旧 marker；迟到响应不污染新图；
 *  ⑤ 切账号隔离：他人/全局行不画；
 *  ⑥ 撤销：DELETE + 读回之后才移除 marker，别的标注不动；
 *  ⑦ 价位线通道不受影响（线还是线，marker 还是 marker，可共存）。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

const H = vi.hoisted(() => ({
  calls: [] as string[],
  /** 存活价位线：handle → createPriceLine 配置 */
  live: new Map<object, Record<string, unknown>>(),
  /** 最近一次 setMarkers 的数组（lightweight-charts 替身记录） */
  markers: [] as Array<Record<string, unknown>>,
  /** `/copilot/annotations` 的响应（可返回 Promise，做竞态用） */
  annotations: (() => Promise.resolve([])) as (url: string) => Promise<unknown>,
  /** DELETE /copilot/annotations/{id} 的响应 */
  revoke: (() => Promise.resolve({ ok: false })) as (url: string) => Promise<unknown>,
  /** `/klines/{symbol}` 的响应（默认两只票的日线，可替换成失败/空数组） */
  klines: ((_: string) =>
    Promise.resolve({
      klines: [
        { date: '2026-03-05', open: 32, high: 33, low: 31, close: 32.93, volume: 1000 },
        { date: '2026-03-06', open: 32.93, high: 34.2, low: 32.5, close: 33.5, volume: 1100 },
      ],
    })) as (url: string) => Promise<unknown>,
  /** 让图层拒绝这批 marker（LWC 抛错）：验证"只有真画出来才算已上图" */
  setMarkersThrows: false,
}))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string, opts?: { method?: string }) => {
    const u = String(url)
    H.calls.push(u)
    if (u.startsWith('/copilot/annotations')) {
      // 撤销走 DELETE（生产代码传 `{ method: 'DELETE' }`），读取走 GET
      if (opts?.method === 'DELETE') return H.revoke(u)
      return H.annotations(u)
    }
    if (u.includes('/klines/')) {
      if (u.includes('/summary')) return Promise.resolve({})
      if (u.includes('/patterns') || u.includes('/wyckoff')) return Promise.resolve({ markers: [] })
      return H.klines(u)
    }
    return Promise.resolve([])
  }),
}))

/**
 * fetchAPI 的 DELETE 版本：生产代码走 `{ method: 'DELETE' }`，替身按 method 分流。
 *
 * 另：`setMarkers` 在 `H.setMarkersThrows` 时对**含时间点标注的批次**抛错 —— 这是 LWC
 * 真实会做的事（`Value is null` 之类），用来验证"图表拒绝了就不能说已上图"。
 */
vi.mock('lightweight-charts', async () => {
  const stub = await import('../../tests/stubs/lightweight-charts')
  const base = stub as unknown as {
    createChart: () => unknown
    CandlestickSeries: unknown
    HistogramSeries: unknown
    LineSeries: unknown
    LineStyle: Record<string, number>
    CrosshairMode: Record<string, number>
  }
  const wrappedCreateChart = () => {
    const chart = base.createChart() as {
      addSeries: () => {
        setData: () => void
        applyOptions: () => void
        createPriceLine: (o: Record<string, unknown>) => Record<string, never>
        removePriceLine: (l: unknown) => void
        priceScale: () => { applyOptions: (o: unknown) => void }
        coordinateToPrice: () => number
        priceToCoordinate: () => null
      }
    }
    const origAdd = chart.addSeries
    chart.addSeries = () => {
      const s = origAdd()
      const origCreate = s.createPriceLine
      s.createPriceLine = (o: Record<string, unknown>) => {
        const handle = origCreate(o)
        H.live.set(handle, o)
        return handle
      }
      const origRemove = s.removePriceLine
      s.removePriceLine = (l: unknown) => {
        H.live.delete(l as object)
        origRemove(l)
      }
      return s
    }
    return chart
  }
  /**
   * 含时间点标注的批次是否应该被"图层"拒绝（模拟 LWC 抛 `Value is null` 之类）。
   * 必须在工厂内部定义：`vi.mock` 的工厂会被提升，引用外部变量会在初始化前访问。
   */
  const rejects = (arr: unknown) =>
    H.setMarkersThrows &&
    Array.isArray(arr) &&
    arr.some((m) => typeof (m as { annotationId?: unknown }).annotationId === 'number')

  return {
    ...base,
    createChart: wrappedCreateChart,
    createSeriesMarkers: (_series: unknown, initial?: unknown) => {
      if (rejects(initial)) throw new Error('Value is null')
      if (Array.isArray(initial)) H.markers = initial as Array<Record<string, unknown>>
      return {
        setMarkers: (next: unknown) => {
          if (rejects(next)) throw new Error('Value is null')
          H.markers = Array.isArray(next) ? (next as Array<Record<string, unknown>>) : []
        },
        detach: () => {},
      }
    },
    LineStyle: { ...base.LineStyle, Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3 },
  }
})

import KlineChart from '@sida/biz-ui/components/KlineChart'
import { AnnotationLinkBar } from '@/components/copilot/AnnotationLinkBar'
import { AnnotationLedger, resetAnnotationLedger } from '@/lib/copilot/annotationLedger'
import { linkConfirmedAnnotation, requestAnnotations } from '@/lib/copilot/annotationLink'
import { revokeAnnotation } from '@/lib/copilot/annotationLink'
import type { AnnotationTarget } from '@/lib/copilot/serverAnnotations'
import { useWorkbenchAnnotations } from '@/hooks/useWorkbenchAnnotations'

const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id }))}.signature`

const annotationMarkers = () =>
  H.markers.filter((m) => typeof m.annotationId === 'number') as Array<{
    annotationId: number
    actionId: number
    time: unknown
    text: string
    color: string
    shape: string
  }>

const priceLineTitles = () => [...H.live.values()].map((l) => String(l.title))

/** 服务端 `GET /copilot/annotations` 的一行（纯时间点标注：price 为 null、ts 是日粒度）。 */
function row(overrides: Record<string, unknown> = {}) {
  return {
    id: 91,
    symbol: '600111',
    market: 'CN',
    period: '1d',
    user_id: 'user-A',
    action_id: 7,
    price: null,
    ts: '2026-03-06',
    label: '复盘点',
    note: null,
    color: 'up',
    source: 'ai',
    created_at: '2026-03-06 15:00:00',
    ...overrides,
  }
}

/** `POST /copilot/actions/{id}/apply` 的响应（ts-only 标注：annotation.price = null）。 */
function applied(overrides: Record<string, unknown> = {}, detail: Record<string, unknown> = {}) {
  return {
    id: 7,
    run_id: null,
    kind: 'annotation',
    status: 'applied',
    payload: { symbol: '600111', market: 'CN', label: '复盘点', ts: '2026-03-06' },
    annotation_id: 91,
    annotation: {
      id: 91,
      symbol: '600111',
      market: 'CN',
      period: '1d',
      price: null,
      ts: '2026-03-06',
      label: '复盘点',
      color: 'up',
      source: 'ai',
      ...detail,
    },
    result: '已标注 600111 @ 2026-03-06：复盘点',
    diff: [],
    can_rollback: true,
    created_at: '2026-03-06 14:59:00',
    applied_at: '2026-03-06 15:00:00',
    ...overrides,
  }
}

function target(overrides: Partial<AnnotationTarget> = {}): AnnotationTarget {
  return {
    owner: 'user-A',
    instrument: 'stock',
    symbol: '600111',
    market: 'CN',
    period: '1d',
    ...overrides,
  }
}

/** 页面等价接线：生产 hook + 真 KlineChart + 真状态条。 */
function Harness({
  symbol = '600111',
  instrument = 'stock' as 'stock' | 'index',
  interval = '1d' as '1d' | '5m' | '1w' | '1mth',
  annotationLayerVisible = true,
}: {
  symbol?: string
  instrument?: 'stock' | 'index'
  interval?: '1d' | '5m' | '1w' | '1mth'
  annotationLayerVisible?: boolean
}) {
  const t = target({ symbol, instrument })
  const { snapshot, priceLines, annotationMarkers: marks, reportMarkerRender } =
    useWorkbenchAnnotations(t)
  return (
    <div>
      <AnnotationLinkBar snapshot={snapshot} onRevoke={(id) => void revokeAnnotation(id)} />
      <KlineChart
        symbol={symbol}
        market="CN"
        instrument={instrument}
        initialInterval={interval as never}
        initialDays={120}
        height={360}
        supportPressure={priceLines}
        annotationMarkers={marks}
        layersVisible={{ annotation: annotationLayerVisible }}
        onAnnotationMarkersDrawn={reportMarkerRender}
      />
    </div>
  )
}

beforeEach(() => {
  localStorage.setItem('token', token('user-A'))
  sessionStorage.clear()
  resetAnnotationLedger()
  H.calls.length = 0
  H.markers = []
  H.live.clear()
  H.annotations = () => Promise.resolve([])
  H.setMarkersThrows = false
  H.klines = () =>
    Promise.resolve({
      klines: [
        { date: '2026-03-05', open: 32, high: 33, low: 31, close: 32.93, volume: 1000 },
        { date: '2026-03-06', open: 32.93, high: 34.2, low: 32.5, close: 33.5, volume: 1100 },
      ],
    })
})

afterEach(() => {
  cleanup()
  resetAnnotationLedger()
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('① 确认 ts-only 标注 → 真实 marker 落在该日（不编价位）', () => {
  it('全链路：apply → 精确回读 → 图上出现该日的 marker，且没有价位线', async () => {
    render(<Harness />)
    await waitFor(() => expect(H.calls.some((u) => u.includes('/klines/600111?'))).toBe(true))
    await waitFor(() => expect(AnnotationLedger.currentTarget()).not.toBeNull())

    H.annotations = () => Promise.resolve([row({ id: 91, ts: '2026-03-06' })])
    let snap = null as ReturnType<typeof AnnotationLedger.snapshot> | null
    await waitFor(async () => {
      snap = await linkConfirmedAnnotation(applied())
      expect(annotationMarkers().map((m) => m.annotationId)).toEqual([91])
    })
    // 图上：一个 marker，落在 2026-03-06，文本带标注 id（可溯源）
    const drawn = annotationMarkers()
    expect(drawn).toHaveLength(1)
    // 图表时间口径按周期而定（日线是 UTC 时间戳）—— 断言它落在**那一天**，不是格式
    expect(
      new Date(Number(drawn[0].time) * 1000).toISOString().slice(0, 10),
    ).toBe('2026-03-06')
    expect(drawn[0].text).toContain('#91')
    expect(drawn[0].actionId).toBe(7)
    // 没有价位线被"编"出来：价位线图层里没有任何标注标题
    expect(priceLineTitles()).toEqual([])
    // 账本：时间点通道有它，价位线通道没有
    expect(snap?.markers.map((m) => m.annotationId)).toEqual([91])
    expect(snap?.lines).toHaveLength(0)
    // 状态条：图表回报之后才能说"已上图"
    await waitFor(() =>
      expect(screen.getByTestId('copilot-annotation-line').getAttribute('data-marker-state')).toBe(
        'drawn',
      ),
    )
    expect(screen.getByTestId('copilot-annotation-marker-count').textContent).toContain('1')
  })
})

describe('② 刷新重读：不重复、不丢', () => {
  it('再读一次仍然只有一个 marker，id 不变', async () => {
    H.annotations = () => Promise.resolve([row({ id: 91 })])
    render(<Harness />)
    await waitFor(() => expect(annotationMarkers().map((m) => m.annotationId)).toEqual([91]))
    await requestAnnotations(target())
    await waitFor(() => expect(annotationMarkers().map((m) => m.annotationId)).toEqual([91]))
    expect(annotationMarkers()).toHaveLength(1)
  })
})

describe('③ 该日没有 K 线（周末）→ 不画，且不说"已上图"', () => {
  it('2026-03-07 是周六（区间内非交易日）：marker 不出现，状态条写"该日没有 K 线，未上图"', async () => {
    // 区间要**跨过**周末：03-05 四、03-06 五、03-09 一 —— 03-07 落在区间内但那天没有 K 线，
    // 这才是"该日没有 K 线"；区间外的日期另有 out-of-range 的说法（见 ③b）。
    H.klines = () =>
      Promise.resolve({
        klines: [
          { date: '2026-03-05', open: 32, high: 33, low: 31, close: 32.93, volume: 1000 },
          { date: '2026-03-06', open: 32.93, high: 34.2, low: 32.5, close: 33.5, volume: 1100 },
          { date: '2026-03-09', open: 33.5, high: 34.4, low: 33.1, close: 34, volume: 900 },
        ],
      })
    H.annotations = () => Promise.resolve([row({ id: 91, ts: '2026-03-07' })])
    render(<Harness />)
    const line = await screen.findByTestId('copilot-annotation-line')
    await waitFor(() => expect(line.getAttribute('data-marker-state')).toBe('skipped'))
    expect(line.getAttribute('data-marker-reason')).toBe('no-bar')
    expect(annotationMarkers()).toHaveLength(0)
    expect(line.textContent).toContain('该日没有 K 线')
  })

  it('③b 早于已加载区间的日期：说"超出已加载的日K范围"，不冒充"该日没有 K 线"', async () => {
    H.annotations = () => Promise.resolve([row({ id: 91, ts: '2026-02-02' })])
    render(<Harness />)
    const line = await screen.findByTestId('copilot-annotation-line')
    await waitFor(() => expect(line.getAttribute('data-marker-reason')).toBe('out-of-range'))
    expect(annotationMarkers()).toHaveLength(0)
    expect(line.textContent).toContain('超出已加载的日K范围')
  })
})

describe('④ 换标的立刻清掉旧 marker；迟到响应不污染新图', () => {
  it('迟到响应（旧标的）不会画到新标的图上', async () => {
    // 用对象属性承接 reject/resolve：`let x: fn | null = null` 在闭包里赋值会被 tsc 判成
    // `null`（CFA 看不到回调里的赋值）→ `x?.()` 报 TS2349，`typecheck:tests` 直接红。
    const pending: { resolve: ((v: unknown) => void) | null } = { resolve: null }
    H.annotations = (url) =>
      url.includes('symbol=600111') && !url.includes('ids=')
        ? new Promise((res) => {
            pending.resolve = res
          })
        : Promise.resolve([])
    const view = render(<Harness symbol="600111" />)
    await waitFor(() => expect(pending.resolve).not.toBeNull())

    // 还没回来就切到另一只票
    view.rerender(<Harness symbol="600519" />)
    await waitFor(() => expect(AnnotationLedger.currentTarget()?.symbol).toBe('600519'))

    // 旧请求这才回来（带一条 600111 的 marker）
    pending.resolve?.([row({ id: 91, symbol: '600111' })])
    await new Promise((r) => setTimeout(r, 50))
    expect(annotationMarkers()).toHaveLength(0)
    const snap = AnnotationLedger.snapshot(target({ symbol: '600519' }))
    expect(snap.markers).toHaveLength(0)
  })
})

describe('⑤ 切账号隔离：他人/全局行不画', () => {
  it('user-B 的行在我这边一条都不画，状态条如实说未上图', async () => {
    H.annotations = () =>
      Promise.resolve([row({ id: 91, user_id: 'user-B' }), row({ id: 92, user_id: null })])
    render(<Harness />)
    await waitFor(() =>
      expect(screen.getByTestId('copilot-annotation-unattributed')).toBeTruthy(),
    )
    expect(annotationMarkers()).toHaveLength(0)
    expect(AnnotationLedger.snapshot(target()).markers).toHaveLength(0)
  })
})

describe('⑥ 撤销：DELETE + 读回之后才移除 marker，别的标注不动', () => {
  it('撤销其中一条：只移除它，另一条 marker 仍在', async () => {
    const twoRows = [
      row({ id: 91, ts: '2026-03-05', label: '复盘点' }),
      row({ id: 92, ts: '2026-03-06', label: '二次确认', action_id: 8 }),
    ]
    H.annotations = () => Promise.resolve(twoRows)
    render(<Harness />)
    await waitFor(() => expect(annotationMarkers()).toHaveLength(2))

    // 服务端：DELETE 成功（revoked + rolled_back），之后读回只剩 92
    H.revoke = () =>
      Promise.resolve({
        ok: true,
        status: 'revoked',
        action_id: 7,
        annotation_id: 91,
        action_status: 'rolled_back',
        symbol: '600111',
        label: '复盘点',
        reason: 'test',
        revoked_at: '2026-03-07 10:00:00',
      })
    H.annotations = () =>
      Promise.resolve([row({ id: 92, ts: '2026-03-06', label: '二次确认', action_id: 8 })])

    await revokeAnnotation(91)
    await waitFor(() => expect(annotationMarkers().map((m) => m.annotationId)).toEqual([92]))
    expect(AnnotationLedger.snapshot(target()).markers.map((m) => m.annotationId)).toEqual([92])
  })
})

describe('⑦ 价位线通道不受影响', () => {
  it('线行与时间点行同时存在：价位线照常，marker 也在，互不冒充', async () => {
    H.annotations = () =>
      Promise.resolve([
        row({ id: 91, ts: '2026-03-06', label: '复盘点' }),
        row({ id: 92, price: 33.5, ts: null, label: '支撑位', action_id: 8 }),
      ])
    render(<Harness />)
    await waitFor(() => expect(annotationMarkers()).toHaveLength(1))
    expect(priceLineTitles()).toEqual(['支撑位'])
    expect(annotationMarkers()[0].annotationId).toBe(91)
    const snap = AnnotationLedger.snapshot(target())
    expect(snap.lines.map((l) => [l.annotationId, l.price])).toEqual([[92, 33.5]])
    expect(snap.markers.map((m) => m.annotationId)).toEqual([91])
  })
})

/**
 * ⑧ K 线没到位（加载失败/空数组）时**不许拿上一只票的 K 线日期**说"已上图"。
 *
 * 复审阻断 ②：`rawKlinesRef` 是 marker 定位的唯一真源。旧实现换标的/失败时不清它，
 * 于是"新标的加载失败"这件事被**前一只票的 K 线**盖住 —— 日期刚好对得上，一条根本没画的
 * 标注被报成已上图。这里要的是：不画、且状态条说"K 线未加载成功，无法确认"。
 */
describe('⑧ K 线未就绪：不按旧 K 线判已上图', () => {
  it('切到新标的且 K 线加载失败 → 不画 marker，状态条说"无法确认"', async () => {
    H.annotations = () => Promise.resolve([row({ id: 91, symbol: '600111', ts: '2026-03-06' })])
    const view = render(<Harness symbol="600111" />)
    await waitFor(() =>
      expect(screen.getByTestId('copilot-annotation-line').getAttribute('data-marker-state')).toBe(
        'drawn',
      ),
    )

    // 新标的：同一天也有时间点标注，但 K 线接口失败
    H.klines = (u) =>
      u.includes('600519') ? Promise.reject(new Error('K线服务不可用')) : Promise.resolve({ klines: [] })
    H.annotations = () =>
      Promise.resolve([
        row({ id: 92, symbol: '600519', ts: '2026-03-06', label: '新标的点', action_id: 8 }),
      ])
    view.rerender(<Harness symbol="600519" />)

    await waitFor(() => expect(AnnotationLedger.currentTarget()?.symbol).toBe('600519'))
    await waitFor(() => expect(annotationMarkers()).toHaveLength(0))
    const line = await screen.findByTestId('copilot-annotation-line')
    expect(line.getAttribute('data-marker-state')).toBe('skipped')
    expect(line.getAttribute('data-marker-reason')).toBe('no-data')
    expect(line.textContent).toContain('无法确认')
    // 账本里也不能留下"已上图"的结论
    const snap = AnnotationLedger.snapshot(target({ symbol: '600519' }))
    expect(snap.markerRender?.drawn).toEqual([])
    expect(snap.markerRender?.skipped.map((s) => s.reason)).toEqual(['no-data'])
  })

  it('K 线返回空数组 → 同样不画、不说已上图', async () => {
    H.klines = () => Promise.resolve({ klines: [] })
    H.annotations = () => Promise.resolve([row({ id: 91, ts: '2026-03-06' })])
    render(<Harness />)
    const line = await screen.findByTestId('copilot-annotation-line')
    await waitFor(() => expect(line.getAttribute('data-marker-state')).toBe('skipped'))
    expect(line.getAttribute('data-marker-reason')).toBe('no-data')
    expect(annotationMarkers()).toHaveLength(0)
  })
})

/**
 * ⑨ 日期只有"哪一天"：**只有日 K 能定位**。
 *
 * 复审阻断 ④：`fundBarTime('2026-03-06','5m')` 按本地零点解析 → 00:00 的分钟时间戳 ——
 * 那不是任何一根分钟 K 线，是给用户编出来的开盘时间。周/月同理（一根周 K 不等于那一天）。
 * 一律不画，并如实说"日期标注只在日 K 上绘制"。
 */
describe('⑨ 分时/周/月不定位日期标注（绝不编 00:00 假时间）', () => {
  it('5分周期：一个时间点 marker 都不画，说明只在日K绘制', async () => {
    H.annotations = () => Promise.resolve([row({ id: 91, ts: '2026-03-06' })])
    render(<Harness interval="5m" />)
    const line = await screen.findByTestId('copilot-annotation-line')
    await waitFor(() => expect(line.getAttribute('data-marker-state')).toBe('skipped'))
    expect(line.getAttribute('data-marker-reason')).toBe('not-daily')
    expect(line.textContent).toContain('只在日K上绘制')
    expect(annotationMarkers()).toHaveLength(0)
    // 关键：没有 00:00 的假时间戳被送到图层
    expect(H.markers.filter((m) => typeof m.annotationId === 'number')).toHaveLength(0)
    expect(AnnotationLedger.snapshot(target()).markerRender?.drawn).toEqual([])
  })

  it('周K同理：不把日期贴到那根周 K 上', async () => {
    H.annotations = () => Promise.resolve([row({ id: 91, ts: '2026-03-06' })])
    render(<Harness interval="1w" />)
    const line = await screen.findByTestId('copilot-annotation-line')
    await waitFor(() => expect(line.getAttribute('data-marker-reason')).toBe('not-daily'))
    expect(annotationMarkers()).toHaveLength(0)
  })
})

/**
 * ⑩ 图层关闭 / 图表拒绝：两种"没画上"的原因必须分开说。
 *
 * 复审阻断 ⑤：图层隐藏是用户自己关的；`render-failed` 是图表把批次拒了。
 * 都不许说成"已上图"，也不许和"该日没有 K 线"混成一个说法。
 */
describe('⑩ 图层关闭与绘制失败：状态诚实', () => {
  it('时间点标注图层关闭 → 不画，说明图层已关闭', async () => {
    H.annotations = () => Promise.resolve([row({ id: 91, ts: '2026-03-06' })])
    render(<Harness annotationLayerVisible={false} />)
    const line = await screen.findByTestId('copilot-annotation-line')
    await waitFor(() => expect(line.getAttribute('data-marker-state')).toBe('skipped'))
    expect(line.getAttribute('data-marker-reason')).toBe('layer-hidden')
    expect(line.textContent).toContain('图层已关闭')
    expect(annotationMarkers()).toHaveLength(0)
  })

  it('图层拒绝这批 marker（LWC 抛错）→ 不能说已上图，报"图表未能绘制"', async () => {
    H.setMarkersThrows = true
    H.annotations = () => Promise.resolve([row({ id: 91, ts: '2026-03-06' })])
    render(<Harness />)
    const line = await screen.findByTestId('copilot-annotation-line')
    await waitFor(() => expect(line.getAttribute('data-marker-state')).toBe('skipped'))
    expect(line.getAttribute('data-marker-reason')).toBe('render-failed')
    expect(line.textContent).toContain('图表未能绘制')
    expect(AnnotationLedger.snapshot(target()).markerRender?.drawn).toEqual([])
  })
})

/**
 * ⑪ 确认落在"正在重新读取"窗口：不许把图上既有标注整批抹掉。
 *
 * 复审阻断 ③：`beginRequest` 会把快照置成 loading 并清空 lines/markers。旧实现
 * `linkConfirmedAnnotation` 拿这份空快照当合并基线 → 图上其余标注整批消失（无提示、
 * 也无法撤销回来）。基线必须用 baseline*（loading 窗口回落到"上次已知的"），
 * 而且那次在途读回落地时也要保留刚确认的这条（增量）。
 */
describe('⑪ 确认撞上在途重读：既有标注不被抹掉', () => {
  it('慢重读在途时确认一条 → 既有价位线/marker 仍在，迟到读回也不抹掉新确认', async () => {
    H.annotations = () =>
      Promise.resolve([
        row({ id: 91, ts: '2026-03-06', label: '复盘点' }),
        row({ id: 92, price: 33.5, ts: null, label: '支撑位', action_id: 8 }),
      ])
    render(<Harness />)
    await waitFor(() => expect(annotationMarkers()).toHaveLength(1))
    expect(priceLineTitles()).toEqual(['支撑位'])

    // 用户点了「重新读取」：全量 GET 挂起（账本进入 loading 窗口）
    let releaseRead: (() => void) | null = null
    H.annotations = (u) =>
      u.includes('ids=')
        ? Promise.resolve([
            row({ id: 93, ts: '2026-03-05', label: '新确认', action_id: 9 }),
          ])
        : new Promise((res) => {
            releaseRead = () =>
              res([
                row({ id: 91, ts: '2026-03-06', label: '复盘点' }),
                row({ id: 92, price: 33.5, ts: null, label: '支撑位', action_id: 8 }),
              ])
          })
    const slowRead = requestAnnotations(target())
    await waitFor(() => expect(releaseRead).not.toBeNull())
    expect(AnnotationLedger.snapshot(target()).phase).toBe('loading')

    // 窗口里确认一条新的时间点标注
    const snap = await linkConfirmedAnnotation(
      applied({
        id: 9,
        annotation_id: 93,
        annotation: {
          id: 93,
          symbol: '600111',
          market: 'CN',
          period: '1d',
          price: null,
          ts: '2026-03-05',
          label: '新确认',
          color: 'up',
          source: 'ai',
        },
        result: '已标注 600111 @ 2026-03-05：新确认',
      }),
    )
    // 既有 91(marker)/92(线) 必须还在，93 也进来了（markers 按日期升序：03-05 在前）
    expect(snap?.lines.map((l) => l.annotationId)).toEqual([92])
    expect(snap?.markers.map((m) => m.annotationId)).toEqual([93, 91])
    expect(priceLineTitles()).toEqual(['支撑位'])
    await waitFor(() => expect(annotationMarkers().map((m) => m.annotationId)).toEqual([93, 91]))

    // 慢读回这才落地（它是在确认之前发出的，读不到 93）：不能把 93 抹掉，也不能复活别的
    releaseRead!()
    await slowRead
    const after = AnnotationLedger.snapshot(target())
    expect(after.markers.map((m) => m.annotationId)).toEqual([93, 91])
    expect(after.lines.map((l) => l.annotationId)).toEqual([92])
    await waitFor(() => expect(annotationMarkers().map((m) => m.annotationId)).toEqual([93, 91]))
  })
})

/**
 * ⑫ 目标变化后状态诚实：换标的不沿用上一张图的落图结论。
 */
describe('⑫ 换标的后重新回报落图结果', () => {
  it('两只票同一天都有标注：新标的也要自己回报"已上图"，不沿用旧结论', async () => {
    H.annotations = () => Promise.resolve([row({ id: 91, symbol: '600111', ts: '2026-03-06' })])
    const view = render(<Harness symbol="600111" />)
    await waitFor(() =>
      expect(screen.getByTestId('copilot-annotation-line').getAttribute('data-marker-state')).toBe(
        'drawn',
      ),
    )

    H.annotations = () =>
      Promise.resolve([
        row({ id: 92, symbol: '600519', ts: '2026-03-06', label: '复盘点', action_id: 8 }),
      ])
    view.rerender(<Harness symbol="600519" />)
    await waitFor(() => expect(annotationMarkers().map((m) => m.annotationId)).toEqual([92]))
    await waitFor(() =>
      expect(screen.getByTestId('copilot-annotation-line').getAttribute('data-marker-state')).toBe(
        'drawn',
      ),
    )
    const snap = AnnotationLedger.snapshot(target({ symbol: '600519' }))
    expect(snap.markerRender?.drawn).toEqual([92])
  })
})
