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
      return Promise.resolve({
        klines: [
          { date: '2026-03-05', open: 32, high: 33, low: 31, close: 32.93, volume: 1000 },
          { date: '2026-03-06', open: 32.93, high: 34.2, low: 32.5, close: 33.5, volume: 1100 },
        ],
      })
    }
    return Promise.resolve([])
  }),
}))

/** fetchAPI 的 DELETE 版本：生产代码走 `{ method: 'DELETE' }`，替身按 method 分流。 */
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
  return {
    ...base,
    createChart: wrappedCreateChart,
    createSeriesMarkers: (_series: unknown, initial?: unknown) => {
      if (Array.isArray(initial)) H.markers = initial as Array<Record<string, unknown>>
      return {
        setMarkers: (next: unknown) => {
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
}: {
  symbol?: string
  instrument?: 'stock' | 'index'
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
        initialInterval={'1d' as never}
        initialDays={120}
        height={360}
        supportPressure={priceLines}
        annotationMarkers={marks}
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
  it('2026-03-07 是周六：marker 不出现，状态条写"该日没有 K 线，未上图"', async () => {
    H.annotations = () => Promise.resolve([row({ id: 91, ts: '2026-03-07' })])
    render(<Harness />)
    await waitFor(() =>
      expect(screen.getByTestId('copilot-annotation-line').getAttribute('data-marker-state')).toBe(
        'skipped',
      ),
    )
    expect(annotationMarkers()).toHaveLength(0)
    expect(screen.getByTestId('copilot-annotation-line').textContent).toContain('该日没有 K 线')
  })
})

describe('④ 换标的立刻清掉旧 marker；迟到响应不污染新图', () => {
  it('迟到响应（旧标的）不会画到新标的图上', async () => {
    let resolveOld: ((v: unknown) => void) | null = null
    H.annotations = (url) =>
      url.includes('symbol=600111') && !url.includes('ids=')
        ? new Promise((res) => {
            resolveOld = res
          })
        : Promise.resolve([])
    const view = render(<Harness symbol="600111" />)
    await waitFor(() => expect(resolveOld).not.toBeNull())

    // 还没回来就切到另一只票
    view.rerender(<Harness symbol="600519" />)
    await waitFor(() => expect(AnnotationLedger.currentTarget()?.symbol).toBe('600519'))

    // 旧请求这才回来（带一条 600111 的 marker）
    resolveOld?.([row({ id: 91, symbol: '600111' })])
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
