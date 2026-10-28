// @vitest-environment jsdom
/**
 * 服务端标注上图 —— **实证测试**（2026-10-28 P2 四收口，对应安全复审驳回 3ceb290f）。
 *
 * 复审要的是"真实 KlineChart + 真实 DOM/画线探针"，不是拿 mock 组件自说自话：
 *
 *  - 图表用**生产组件** `@sida/biz-ui/components/KlineChart`（只把 canvas 层
 *    `lightweight-charts` 换成仓库既有的无副作用替身，见 vite.config test.alias）；
 *  - 画线探针 = 替身把 `createPriceLine/removePriceLine` 记进**存活集合**，
 *    断言的是"图上此刻有几条线、什么价、什么标题"（不是调用计数 —— 先删后建是常态）；
 *  - 接线用**生产 hook** `useWorkbenchAnnotations`（页面用的同一个），不复制一套逻辑；
 *  - 网络只替 `fetchAPI`，其余每一行都是生产代码。
 *
 * 覆盖四条红线：
 *  ① 单一通道：图表自己不再拉/画 `/copilot/annotations`；同一价位只画一条；
 *  ② 切标的/周期立刻清旧线 + 迟到响应不污染；
 *  ③ 非日周期确认不谎称已上图；
 *  ④ 用 apply 响应的 annotation_id 精确关联，核对属主/标的/市场/周期/动作，
 *     000001 这类指数/个股同号默认拒画；不再有客户端 WeakSet/本地信封当凭证。
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
  /** 每次请求的 URL（顺序） */
  calls: [] as string[],
  /** 存活价位线：handle → createPriceLine 配置 */
  live: new Map<object, Record<string, unknown>>(),
  removed: [] as unknown[],
  /** `/copilot/annotations` 的响应：函数可返回 Promise（做竞态用） */
  annotations: (() => Promise.resolve([])) as (url: string) => Promise<unknown>,
  /** `/klines/{symbol}/summary` 的响应（图表自取的图层数据，如 unlock_levels） */
  summary: (() => ({})) as () => Record<string, unknown>,
}))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string) => {
    const u = String(url)
    H.calls.push(u)
    if (u.startsWith('/copilot/annotations')) return H.annotations(u)
    if (u.includes('/klines/')) {
      if (u.includes('/summary')) return Promise.resolve(H.summary())
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

vi.mock('lightweight-charts', async () => {
  const stub = await import('../../tests/stubs/lightweight-charts')
  const base = stub as unknown as {
    createChart: () => unknown
    CandlestickSeries: unknown
    HistogramSeries: unknown
    LineSeries: unknown
    createSeriesMarkers: () => unknown
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
        H.removed.push(l)
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
    LineStyle: { ...base.LineStyle, Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3 },
  }
})

import KlineChart from '@sida/biz-ui/components/KlineChart'
import { AnnotationLinkBar } from '@/components/copilot/AnnotationLinkBar'
import {
  AnnotationLedger,
  currentAccountId,
  resetAnnotationLedger,
} from '@/lib/copilot/annotationLedger'
import { linkConfirmedAnnotation, requestAnnotations } from '@/lib/copilot/annotationLink'
import {
  annotationsUrl,
  parseAnnotationsResponse,
  type AnnotationTarget,
} from '@/lib/copilot/serverAnnotations'
import { useWorkbenchAnnotations } from '@/hooks/useWorkbenchAnnotations'

const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id }))}.signature`

/** 主图上"标注价位线"的存活集合（标题取自 label —— 也就是服务端行的 label）。 */
const noteLines = () =>
  [...H.live.values()].filter((l) =>
    ['支撑位', '阻力位', '支撑 32.8', '阻力 36.5', '支撑', '阻力'].includes(String(l.title)),
  )

const labels = () => noteLines().map((l) => `${String(l.title)}@${String(l.price)}`)

/** 服务端 `GET /copilot/annotations` 的真实行形状（含 2026-10-28 新增的回显字段）。 */
function row(overrides: Record<string, unknown> = {}) {
  return {
    id: 91,
    symbol: '600111',
    market: 'CN',
    period: '1d',
    user_id: 'user-A',
    action_id: 7,
    price: 32.8,
    ts: null,
    label: '支撑位',
    note: null,
    color: 'up',
    source: 'ai',
    created_at: '2026-10-28 10:00:00',
    ...overrides,
  }
}

/** apply 响应（`actions.apply_action` 新契约：annotation_id + 落库明细）。 */
function applied(overrides: Record<string, unknown> = {}) {
  const detail = {
    id: 91,
    symbol: '600111',
    market: 'CN',
    period: '1d',
    price: 32.8,
    ts: null,
    label: '支撑位',
    color: 'up',
    source: 'ai',
  }
  return {
    id: 7,
    run_id: null,
    kind: 'annotation',
    status: 'applied',
    payload: { symbol: '600111', market: 'CN', label: '支撑位', price: 32.8 },
    annotation_id: 91,
    annotation: detail,
    result: '已标注 600111 @ 32.8：支撑位',
    diff: [],
    can_rollback: false,
    created_at: '2026-10-28 09:59:00',
    applied_at: '2026-10-28 10:00:00',
    ...overrides,
  }
}

function target(overrides: Partial<AnnotationTarget> = {}): AnnotationTarget {
  return {
    owner: currentAccountId() ?? '',
    instrument: 'stock',
    symbol: '600111',
    market: 'CN',
    period: '1d',
    ...overrides,
  }
}

/**
 * 页面等价接线：`useWorkbenchAnnotations`（生产 hook）+ 真 KlineChart。
 * 只回显状态，便于断言"画了什么/说了什么"同源。
 */
function Harness({
  symbol = '600111',
  period = '1d',
  instrument = 'stock' as 'stock' | 'index',
  market = 'CN',
}: {
  symbol?: string
  period?: string
  instrument?: 'stock' | 'index'
  market?: string
}) {
  const t = target({ symbol, period, instrument, market })
  const { snapshot, priceLines } = useWorkbenchAnnotations(t)
  return (
    <div>
      <div data-testid="phase">{snapshot.phase}</div>
      <div data-testid="message">{snapshot.message}</div>
      <div data-testid="notice">{snapshot.notice}</div>
      <KlineChart
        symbol={symbol}
        market={market}
        instrument={instrument}
        initialInterval={period as never}
        initialDays={120}
        height={360}
        supportPressure={priceLines}
      />
    </div>
  )
}

const annotationCalls = () => H.calls.filter((u) => u.startsWith('/copilot/annotations'))

beforeEach(() => {
  localStorage.setItem('token', token('user-A'))
  sessionStorage.clear()
  resetAnnotationLedger()
  H.calls.length = 0
  H.live.clear()
  H.removed.length = 0
  H.annotations = () => Promise.resolve([])
  H.summary = () => ({})
})

afterEach(() => {
  cleanup()
  resetAnnotationLedger()
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

// ── ① 单一通道：图表不再自取自画 ────────────────────────────────────────────
describe('① 单一通道（重复/未确认上图的根因）', () => {
  it('单独渲染真 KlineChart：即使服务端有标注行，图表也不发标注请求、不画任何标注线', async () => {
    H.annotations = () =>
      Promise.resolve([
        row({ id: 91, label: '支撑位', price: 32.8 }),
        row({ id: 92, label: '阻力位', price: 36.5, color: 'down' }),
      ])

    render(<KlineChart symbol="600111" market="CN" instrument="stock" initialDays={120} height={360} />)
    // 等主图取数完成（图表渲染完才会有画线动作）
    await waitFor(() => expect(H.calls.some((u) => u.includes('/klines/600111'))).toBe(true))
    await new Promise((r) => setTimeout(r, 50))

    expect(annotationCalls()).toHaveLength(0) // 图表不再自己拉标注
    expect(noteLines()).toHaveLength(0) // 也不会把"全部行"画上去
  })

  it('页面接线画的服务端行不会与图表自己的通道重复：两条行 → 图上正好两条线', async () => {
    H.annotations = () =>
      Promise.resolve([
        row({ id: 91, label: '支撑位', price: 32.8 }),
        row({ id: 92, label: '阻力位', price: 36.5, color: 'down' }),
      ])

    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })
    expect(labels()).toEqual(['支撑位@32.8', '阻力位@36.5'])
    // 会话里也没有旧实现的 "🤖" 标记线（旧通道）
    expect([...H.live.values()].some((l) => String(l.title).includes('🤖'))).toBe(false)
  })

  it('重复价位（连点/历史重复）合并成一条：3 行 → 2 条线，并如实说明合并了几条', async () => {
    H.annotations = () =>
      Promise.resolve([
        row({ id: 91, label: '支撑位', price: 32.8 }),
        row({ id: 93, label: '支撑位', price: 32.8 }),
        row({ id: 92, label: '阻力位', price: 36.5, color: 'down' }),
      ])

    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })
    expect(labels()).toEqual(['支撑位@32.8', '阻力位@36.5'])
    await waitFor(() =>
      expect(screen.getByTestId('message').textContent).toContain('合并了 1 条同价重复行'),
    )
  })
})

// ── ② 切标的/周期：立刻清旧线 + 迟到响应不污染 ──────────────────────────────
describe('② 切换与竞态', () => {
  it('切标的：旧线立刻消失（新请求未返回前就是空的），迟到响应不会画回来', async () => {
    let resolveB: (v: unknown) => void = () => {}
    H.annotations = (url) => {
      if (url.includes('symbol=600111')) {
        return new Promise((resolve) => setTimeout(() => resolve([row()]), 10))
      }
      // B 的请求永远不返回（模拟慢网络/断链）
      return new Promise((resolve) => {
        resolveB = resolve
      })
    }

    const view = render(<Harness symbol="600111" />)
    await waitFor(() => expect(noteLines()).toHaveLength(1))

    view.rerender(<Harness symbol="002636" />)
    // **不需要等网络**：换 key 后快照即为空，图上的旧线同步被清掉
    await waitFor(() => expect(noteLines()).toHaveLength(0))
    expect(screen.getByTestId('phase').textContent).not.toBe('ready')

    // B 的响应迟到且带着"别人标的的行" → 目标核对拒掉，不上图
    resolveB([row({ symbol: '600111' })])
    await new Promise((r) => setTimeout(r, 20))
    expect(noteLines()).toHaveLength(0)
  })

  it('切周期：旧线立刻消失，且不会把 1d 的线留在 5m 图上', async () => {
    H.annotations = (url) => Promise.resolve(url.includes('period=5m') ? [] : [row()])

    const view = render(<Harness period="1d" />)
    await waitFor(() => expect(noteLines()).toHaveLength(1))

    view.rerender(<Harness period="5m" />)
    await waitFor(() => expect(noteLines()).toHaveLength(0))
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('idle'))
  })

  it('跨账号：换账号后同标的的同一条服务端行不会沿用上一个账号的图', async () => {
    H.annotations = () => Promise.resolve([row({ user_id: 'user-A' })])

    const view = render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(1))

    localStorage.setItem('token', token('user-B'))
    view.rerender(<Harness />)
    // B 账号读同一行（mock 仍是 A 的行）→ 属主不符，拒画
    await waitFor(() => expect(noteLines()).toHaveLength(0))
    await waitFor(() => expect(screen.getByTestId('message').textContent).toContain('其他账号'))
  })

  it('A→B→A 快速切换：先发的 A 慢响应不许覆盖后发的 A 结果', async () => {
    let firstA = true
    H.annotations = () => {
      if (firstA) {
        firstA = false
        // 第一次 A：慢，返回一条**旧**价位的线
        return new Promise((resolve) => setTimeout(() => resolve([row({ id: 90, price: 30.0 })]), 60))
      }
      return Promise.resolve([row({ id: 91, price: 32.8 })])
    }

    const view = render(<Harness symbol="600111" />)
    view.rerender(<Harness symbol="002636" />)
    view.rerender(<Harness symbol="600111" />)

    await waitFor(() => expect(noteLines()).toHaveLength(1), { timeout: 3000 })
    await new Promise((r) => setTimeout(r, 100)) // 等慢响应回来
    expect(labels()).toEqual(['支撑位@32.8']) // 是"后发的那次"的结果，不是 30.0
  })
})

// ── ③ 非日周期确认：不假称已上图 ────────────────────────────────────────────
describe('③ 周期诚实', () => {
  it('在 5m 图上确认、服务端落在 1d：不画，并明确说"落库在 1d / 当前 5m / 未上图"', async () => {
    // 5m 图上先有一条自己的 5m 标注（30.0）；确认落库的是 1d / 32.8
    H.annotations = () => Promise.resolve([row({ period: '5m', price: 30.0 })])

    render(<Harness period="5m" />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('ready'))
    expect(labels()).toEqual(['支撑位@30'])

    const snap = await linkConfirmedAnnotation(applied()) // apply 明细：period=1d / 32.8
    const notice = snap?.notice ?? ''
    expect(notice).toContain('落库在 1d')
    expect(notice).toContain('5m')
    expect(notice).toContain('未上图')
    expect(notice).not.toContain('已确认并上图')
    // 5m 图上仍是它自己那条 30.0，没有把 1d 的 32.8 硬画上来
    expect(labels()).toEqual(['支撑位@30'])
    expect(AnnotationLedger.snapshot(target({ period: '5m' })).annotationIds).toEqual([91])
  })

  it('周期一致时确认成功并上图（对照：同一链路必须真的能画）', async () => {
    H.annotations = () => Promise.resolve([row({ period: '1d' })])

    render(<Harness period="1d" />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('ready'))

    const snap = await linkConfirmedAnnotation(applied())
    expect(snap?.notice ?? '').toContain('已确认并上图')
    await waitFor(() => expect(noteLines()).toHaveLength(1))
    expect(labels()).toEqual(['支撑位@32.8'])
  })
})

// ── ④ 精确关联 + 指数/个股分离 ──────────────────────────────────────────────
describe('④ 精确身份与目标（不用 label+price 猜）', () => {
  it('确认后按 annotation_id 精确回读：URL 带 ids/market_strict/mine_only/action_linked', async () => {
    H.annotations = () => Promise.resolve([row({ id: 91 })])

    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('ready'))
    H.calls.length = 0

    await linkConfirmedAnnotation(applied({ annotation_id: 91 }))

    const call = annotationCalls().at(-1) ?? ''
    expect(call).toContain('ids=91')
    expect(call).toContain('market_strict=1')
    expect(call).toContain('mine_only=1')
    expect(call).toContain('action_linked=1')
    expect(call).toContain('symbol=600111')
    expect(call).toContain('period=1d')
  })

  it('label+price 相同但不是同一条标（id 不同）→ 不画：关联只认 annotation_id', async () => {
    // 读回里只有 #92，label/price 与确认响应的 #91 完全一致 —— 旧实现会认成同一条
    H.annotations = () => Promise.resolve([row({ id: 92, price: 32.8, label: '支撑位' })])

    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('ready'))

    const snap = await linkConfirmedAnnotation(applied({ annotation_id: 91 }))
    expect(snap?.notice ?? '').toContain('#91')
    expect(snap?.notice ?? '').toContain('未画线')
    // 图上仍只有服务端真实返回的 #92（按 id 核对，没拿同价的 #92 冒充 #91）
    expect(AnnotationLedger.snapshot(target()).annotationIds).toEqual([92])
    expect(labels()).toEqual(['支撑位@32.8'])
  })

  it('行没有确认动作（action_id=null）→ 不画并说明"不是用户确认画上去的"', async () => {
    H.annotations = () => Promise.resolve([row({ action_id: null })])

    render(<Harness />)
    await waitFor(() =>
      expect(screen.getByTestId('message').textContent).toContain('没有对应的确认动作'),
    )
    expect(noteLines()).toHaveLength(0)
    expect(screen.getByTestId('phase').textContent).toBe('refused')
  })

  it('全局行（user_id=null）→ 不画', async () => {
    H.annotations = () => Promise.resolve([row({ user_id: null })])
    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('message').textContent).toContain('全局行'))
    expect(noteLines()).toHaveLength(0)
  })

  it('后端缺字段（无 symbol/market/period/user_id）→ 整行丢弃并计数，不画', async () => {
    H.annotations = () =>
      Promise.resolve([
        { id: 91, price: 32.8, ts: null, label: '支撑位', color: 'up', source: 'ai', created_at: '' },
        { id: 92, price: 36.5, ts: null, label: '阻力位', color: 'down', source: 'ai', created_at: '' },
      ])

    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('idle'))
    expect(noteLines()).toHaveLength(0)
    // 解析层单独再钉一次：缺回显字段必丢弃
    const parsed = parseAnnotationsResponse([
      { id: 91, label: '支撑位', price: 32.8, source: 'ai' },
    ])
    expect(parsed.ok && parsed.value.rows).toHaveLength(0)
    expect(parsed.ok && parsed.value.dropped).toBe(1)
  })

  it('apply 响应没有 annotation_id（旧后端）→ 明确拒绝，不用 label+price 兜底', async () => {
    H.annotations = () => Promise.resolve([row()])
    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('ready'))

    const legacy = applied()
    delete (legacy as Record<string, unknown>).annotation_id
    const snap = await linkConfirmedAnnotation(legacy)
    expect(snap?.notice ?? '').toContain('annotation_id')
    // 没有 id 就不发"精确读取"，也不改变图上的状态
    expect(annotationCalls().filter((u) => u.includes('ids='))).toHaveLength(0)
    expect(AnnotationLedger.snapshot(target()).annotationIds).toEqual([91])
  })

  it('000858 个股读取服务端 stock 行并画线，不能画同号指数行', async () => {
    H.annotations = () => Promise.resolve([
      row({ id: 101, symbol: '000858', market: 'SZ', instrument: 'stock', price: 12.5 }),
      row({ id: 102, symbol: '000858', market: 'CN', instrument: 'index', price: 3000 }),
    ])
    render(<Harness symbol="000858" />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('ready'))
    await waitFor(() => expect(noteLines()).toHaveLength(1))
    expect(noteLines()[0].price).toBe(12.5)
    expect(annotationCalls()[0]).toContain('instrument=stock')
  })

  it('000001 缺少类型的历史行拒画', async () => {
    H.annotations = () => Promise.resolve([row({ symbol: '000001', price: 12.5 })])
    render(<Harness symbol="000001" />)

    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('refused'))
    expect(screen.getByTestId('message').textContent).toContain('标的类型')
    expect(annotationCalls()).toHaveLength(1) // 请求严格类型，缺失类型的历史行仍不画
    expect(noteLines()).toHaveLength(0)
  })

  // 000002/000003 也同时是上证系列指数（A股指数/B股指数）与深市个股（万科A…）：
  // 旧实现只拒"名单里已知撞号"的代码，这两个会当成普通深市个股放过去 —— 现在整段拒。
  it('000002 / 000003 个股页同样默认拒画（000xxx 整段同号，不是名单制）', async () => {
    for (const symbol of ['000002', '000003']) {
      H.annotations = () => Promise.resolve([row({ symbol, price: 9.9 })])
      const view = render(<Harness symbol={symbol} />)

      await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('refused'))
      expect(screen.getByTestId('message').textContent).toContain('标的类型')
      expect(annotationCalls().length).toBeGreaterThan(0)
      expect(noteLines()).toHaveLength(0)
      view.unmount()
    }
  })

  it('指数图不画个股标注，也不发标注请求', async () => {
    H.annotations = () => Promise.resolve([row({ symbol: '000001' })])
    render(<Harness symbol="000001" instrument="index" />)

    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('refused'))
    expect(screen.getByTestId('message').textContent).toContain('指数')
    expect(annotationCalls()).toHaveLength(0)
    expect(noteLines()).toHaveLength(0)
  })

  // 2026-10-28 复审驳回 ①（更新旧断言：字符串相等 → **可证明的同一标的**）：
  // 旧用例断言「行是 SH、页面 CN ⇒ 一律不画」，那正是"确认成功但图上没有线"的根因。
  // 现在 600519 由代码可证明属于 SH，CN 页与 SH 行是同一只股票 ⇒ 必须画；
  // 而可证明矛盾（600519 + SZ）仍然拒画。两边都要钉住，安全不能靠"放宽"换来。
  it('市场按可证映射：600519 的 SH 行在 CN 页正常上图（放宽的是能证明的交易所，不是严格模式）', async () => {
    H.annotations = () =>
      Promise.resolve([row({ symbol: '600519', market: 'SH', price: 1680.5 })])

    render(<Harness symbol="600519" market="CN" />)

    await waitFor(() => expect(noteLines()).toHaveLength(1), { timeout: 3000 })
    expect(screen.getByTestId('phase').textContent).toBe('ready')
    expect(labels()).toEqual(['支撑位@1680.5'])
    // 读取仍是严格模式：market_strict=1 照发（服务端按可证市场集合过滤）
    expect(annotationCalls()[0]).toContain('market=CN')
    expect(annotationCalls()[0]).toContain('market_strict=1')
  })

  it('反方向同样可证：SH 页读 CN 行也上图（同一只股票，不是"CN 特权"）', async () => {
    H.annotations = () =>
      Promise.resolve([row({ symbol: '600519', market: 'CN', price: 1680.5 })])

    render(<Harness symbol="600519" market="SH" />)

    await waitFor(() => expect(noteLines()).toHaveLength(1), { timeout: 3000 })
    expect(labels()).toEqual(['支撑位@1680.5'])
    expect(annotationCalls()[0]).toContain('market=SH')
  })

  it('可证明矛盾的市场（600519 属 SH，行写 SZ）→ 拒画并说明"无法证明是同一只标的"', async () => {
    H.annotations = () =>
      Promise.resolve([row({ symbol: '600519', market: 'SZ', price: 1680.5 })])

    render(<Harness symbol="600519" market="CN" />)

    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('refused'))
    expect(screen.getByTestId('message').textContent).toContain('无法证明是同一只标的')
    expect(noteLines()).toHaveLength(0)
  })

  it('SH 页遇到 600519 的 SZ 行同样拒画（矛盾在任一方向都不画）', async () => {
    H.annotations = () =>
      Promise.resolve([row({ symbol: '600519', market: 'SZ', price: 1680.5 })])

    render(<Harness symbol="600519" market="SH" />)

    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('refused'))
    expect(screen.getByTestId('message').textContent).toContain('无法证明是同一只标的')
    expect(noteLines()).toHaveLength(0)
  })

  it('000001 同号代码的歧义不会被市场映射救活：行是 SH 也拒画，且不发请求', async () => {
    H.annotations = () =>
      Promise.resolve([row({ symbol: '000001', market: 'SH', price: 3200 })])

    render(<Harness symbol="000001" market="CN" />)

    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('refused'))
    expect(screen.getByTestId('message').textContent).toContain('标的类型')
    expect(annotationCalls()).toHaveLength(1) // 严格请求后逐行拒绝缺失类型的历史行
    expect(noteLines()).toHaveLength(0)
  })

  it('客户端没有"验签后画线"的入口：本地价位数组不会被当成标注（无 WeakSet 依赖）', async () => {
    H.annotations = () => Promise.resolve([])
    // 直接把自造价位交给账本/图表都画不出来：账本没有写入本地价位的 API，
    // 图表只认 supportPressure（本用例不传 = 不画）。
    const forged = [{ price: 1.23, kind: 'support' as const, label: '伪造支撑' }]
    render(
      <KlineChart symbol="600111" market="CN" instrument="stock" initialDays={120} height={360} />,
    )
    expect(forged).toHaveLength(1)
    await waitFor(() => expect(H.calls.some((u) => u.includes('/klines/600111'))).toBe(true))
    await new Promise((r) => setTimeout(r, 30))
    expect(noteLines()).toHaveLength(0)
    expect(AnnotationLedger.snapshot(target()).lines).toHaveLength(0)
  })
})

// ── 读取 URL / 账本基础行为（纯函数级）─────────────────────────────────────
describe('读取 URL 与账本', () => {
  it('默认严格模式参数齐全，ids 只在精确读取时出现', () => {
    const url = annotationsUrl(target())
    expect(url).toContain('symbol=600111')
    expect(url).toContain('market=CN')
    expect(url).toContain('period=1d')
    expect(url).toContain('market_strict=1')
    expect(url).toContain('mine_only=1')
    expect(url).toContain('action_linked=1')
    expect(url).not.toContain('ids=')
    expect(annotationsUrl(target(), { ids: [91, 92] })).toContain('ids=91%2C92')
  })

  it('未登录不发请求（避免 401 触发全局登出）', async () => {
    localStorage.removeItem('token')
    const snap = await requestAnnotations(target({ owner: '' }))
    expect(snap.phase).toBe('refused')
    expect(snap.message).toContain('请先登录')
    expect(annotationCalls()).toHaveLength(0)
  })

  it('读取失败：清空旧线、给出可重试原因', async () => {
    H.annotations = () => Promise.reject(new Error('网络异常'))
    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('error'))
    expect(screen.getByTestId('message').textContent).toContain('读取服务端标注失败')
    expect(noteLines()).toHaveLength(0)
  })
})

// ── ⑤ 确认写进「当前视图键」（页面 CN / 服务端 SH），notice 同源 ────────────────
//
// 复审阻断：`linkConfirmedAnnotation` 用**服务端落库市场**（SH）建目标，然后拿它当账本键
// 写入 —— 页面订阅的是 `?market=CN` 的键，于是「确认成功」既不上图也没有任何提示，
// 服务端键里却多了一条界面永远读不到的线。账本键必须是**页面当前视图**。
describe('⑤ 确认写入视图键（CN 页面 / SH 落库）', () => {
  /** apply 响应：落库明细是交易所市场（SH）—— 提议里写的就是交易所。 */
  function appliedSh(overrides: Record<string, unknown> = {}) {
    return applied({
      annotation: {
        id: 91,
        symbol: '600519',
        market: 'SH',
        period: '1d',
        price: 1680.5,
        ts: null,
        label: '支撑位',
        color: 'up',
        source: 'ai',
      },
      payload: { symbol: '600519', market: 'SH', label: '支撑位', price: 1680.5 },
      ...overrides,
    })
  }

  it('SH 落库 + CN 页面：线画进 CN 视图键并上图，notice 也能被页面读到', async () => {
    H.annotations = () =>
      Promise.resolve([row({ symbol: '600519', market: 'SH', price: 1680.5 })])
    render(<Harness symbol="600519" market="CN" />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('ready'))
    H.calls.length = 0

    const snap = await linkConfirmedAnnotation(appliedSh())

    // 精确回读仍按服务端市场（SH）发起 —— 视图键只用于落账本，不改请求语义
    expect(annotationCalls().at(-1) ?? '').toContain('market=SH')
    // 账本：当前视图键（CN）拿到这条线；服务端键（SH）不该被写成"可画"
    expect(AnnotationLedger.snapshot(target({ symbol: '600519', market: 'CN' })).annotationIds).toEqual([91])
    expect(AnnotationLedger.snapshot(target({ symbol: '600519', market: 'SH' })).annotationIds).toEqual([])
    // notice 与线同源：都挂在视图键上，页面状态条读得到
    expect(snap?.notice ?? '').toContain('已确认并上图')
    expect(snap?.key).toBe(target({ symbol: '600519', market: 'CN' }).owner + '|stock|600519|CN|1d')
    expect(screen.getByTestId('notice').textContent).toContain('已确认并上图')
    await waitFor(() => expect(noteLines()).toHaveLength(1))
    expect(labels()).toEqual(['支撑位@1680.5'])
    // 页面自己那条 CN 行还在（没有被"服务端键的快照"覆盖丢掉）
    expect(AnnotationLedger.snapshot(target({ symbol: '600519', market: 'CN' })).lines).toHaveLength(1)
  })

  it('视图里已有一条同标的的线时，确认新的一条是**合并**而不是替换', async () => {
    H.annotations = (url) =>
      Promise.resolve(
        url.includes('ids=')
          ? [row({ id: 91, symbol: '600519', market: 'SH', price: 1680.5 })]
          : [row({ id: 90, symbol: '600519', market: 'CN', price: 1600, label: '阻力位', color: 'down' })],
      )
    render(<Harness symbol="600519" market="CN" />)
    await waitFor(() => expect(noteLines()).toHaveLength(1))
    expect(labels()).toEqual(['阻力位@1600'])

    await linkConfirmedAnnotation(appliedSh())

    await waitFor(() => expect(noteLines()).toHaveLength(2))
    expect(labels()).toEqual(['阻力位@1600', '支撑位@1680.5'])
    expect(
      AnnotationLedger.snapshot(target({ symbol: '600519', market: 'CN' })).annotationIds,
    ).toEqual([90, 91])
  })

  it('确认过程中切到别的标的：不把线写进旧视图键，当前视图明说"未上图"', async () => {
    H.annotations = (url) => {
      if (url.includes('ids=')) {
        return new Promise((resolve) => setTimeout(() => resolve([row({
          id: 91, symbol: '600519', market: 'SH', price: 1680.5,
        })]), 30))
      }
      return Promise.resolve([])
    }
    const view = render(<Harness symbol="600519" market="CN" />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).not.toBe('loading'))

    const pending = linkConfirmedAnnotation(appliedSh())
    view.rerender(<Harness symbol="002636" />)
    await pending

    expect(noteLines()).toHaveLength(0)
    expect(
      AnnotationLedger.snapshot(target({ symbol: '600519', market: 'CN' })).lines,
    ).toHaveLength(0)
    expect(
      AnnotationLedger.snapshot(target({ symbol: '002636', market: 'CN' })).lines,
    ).toHaveLength(0)
    expect(screen.getByTestId('notice').textContent).toContain('未在当前图上显示')
    expect(screen.getByTestId('notice').textContent).toContain('600519')
  })

  it('确认后切账号：视图键带账号，另一个账号读不到这条线', async () => {
    H.annotations = () => Promise.resolve([row({ symbol: '600519', market: 'SH', price: 1680.5 })])
    const view = render(<Harness symbol="600519" market="CN" />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('ready'))
    await linkConfirmedAnnotation(appliedSh())
    await waitFor(() => expect(noteLines()).toHaveLength(1))

    localStorage.setItem('token', token('user-B'))
    view.rerender(<Harness symbol="600519" market="CN" />)
    await waitFor(() => expect(noteLines()).toHaveLength(0))
  })

  it('确认后才回来的旧读取不会把刚确认的线覆盖掉', async () => {
    let resolveRead: (v: unknown) => void = () => {}
    let firstRead = true
    H.annotations = (url) => {
      if (url.includes('ids=')) {
        return Promise.resolve([row({ symbol: '600519', market: 'SH', price: 1680.5 })])
      }
      // 首次"页面读取"挂起：它早于 apply 发出，看不清刚确认的那一行
      if (firstRead) {
        firstRead = false
        return new Promise((resolve) => {
          resolveRead = resolve
        })
      }
      return Promise.resolve([])
    }

    render(<Harness symbol="600519" market="CN" />)
    await linkConfirmedAnnotation(appliedSh())
    await waitFor(() => expect(noteLines()).toHaveLength(1))

    resolveRead([]) // 旧读取迟到（不含新确认的行）
    await new Promise((r) => setTimeout(r, 40))
    expect(noteLines()).toHaveLength(1)
    expect(
      AnnotationLedger.snapshot(target({ symbol: '600519', market: 'CN' })).annotationIds,
    ).toEqual([91])
  })

  it('真实状态条：确认后旧价线被替换、金额/标题不被误读，且明确写出"撤销不可用"', async () => {
    H.annotations = () => Promise.resolve([row({ symbol: '600519', market: 'SH', price: 1680.5 })])
    render(<Harness symbol="600519" market="CN" />)
    await waitFor(() => expect(screen.getByTestId('phase').textContent).toBe('ready'))

    const snap = await linkConfirmedAnnotation(appliedSh())
    expect(snap).not.toBeNull()

    cleanup()
    render(<AnnotationLinkBar snapshot={snap!} />)
    // 状态条读的就是**当前视图键**的快照：条数与服务端标注 id 一致
    expect(screen.getByTestId('copilot-annotation-ids').textContent).toContain('#91')
    expect(screen.getByTestId('copilot-annotation-action-ids').textContent).toContain('#7')
    expect(screen.getAllByTestId('copilot-annotation-line')).toHaveLength(1)
    // notice（"已确认并上图" + 市场写法说明）+ 撤销不可用都如实写在这里
    const notice = screen.getByTestId('copilot-annotation-notice').textContent ?? ''
    expect(notice).toContain('已确认并上图')
    expect(notice).toContain('落库市场 SH')
    expect(screen.getByTestId('copilot-annotation-undo-note').textContent).toContain('撤销不可用')
  })
})

// ── ⑥ 图表把「已确认标注」与自带图层（unlock_levels）合并，不互相覆盖 ─────────
//
// 复审阻断：`supportPressure` 一传（哪怕传空数组），图表就把自己从
// `/klines/{symbol}/summary` 取到的 `unlock_levels`（筹码套牢/支撑压力）丢掉 ——
// 页面上挂了参谋标注，图层的支撑压力线就整批消失；同一个价位还会画两条重合虚线。
describe('⑥ 图表价位线合并（confirmed annotations + unlock_levels）', () => {
  const chipLines = () => [...H.live.values()].filter((l) => String(l.title).startsWith('筹码'))

  it('参谋标注与图层价线同时上图；同价位同向只画一条（标注文案优先）', async () => {
    H.summary = () => ({
      unlock_levels: [
        { price: 32.8, kind: 'support', label: '筹码支撑 32.8' },
        { price: 36.5, kind: 'pressure', label: '筹码压力 36.5' },
        { price: 40.0, kind: 'pressure', label: '筹码压力 40.0' },
      ],
    })
    H.annotations = () =>
      Promise.resolve([
        row({ id: 91, label: '支撑位', price: 32.8 }),
        row({ id: 92, label: '阻力位', price: 36.5, color: 'down' }),
      ])

    render(<Harness />)
    // 图表自取 summary 的图层线 + 页面接线画的标注线都在
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })
    await waitFor(() => expect(chipLines()).toHaveLength(1), { timeout: 3000 })

    expect(labels()).toEqual(['支撑位@32.8', '阻力位@36.5'])
    // 32.8/36.5 被标注占用（同价同向去重，标注文案优先），40 只来自图层
    expect(chipLines().map((l) => `${String(l.title)}@${String(l.price)}`)).toEqual([
      '筹码压力 40.0@40',
    ])
  })

  it('没有参谋标注时，图层价线照旧全画（空数组不等于"接管"）', async () => {
    H.summary = () => ({
      unlock_levels: [
        { price: 32.8, kind: 'support', label: '筹码支撑 32.8' },
        { price: 36.5, kind: 'pressure', label: '筹码压力 36.5' },
      ],
    })
    H.annotations = () => Promise.resolve([])

    render(<Harness />)
    await waitFor(() => expect(chipLines()).toHaveLength(2), { timeout: 3000 })
    expect(noteLines()).toHaveLength(0)
  })
})
