// @vitest-environment jsdom
/**
 * 标注撤销 —— 真实交互（2026-10-29, P2 撤销闭合）。
 *
 * 后端新增 `DELETE /api/copilot/annotations/{id}?reason=`（只撤本人已确认的标注）。
 * 本文件用**生产组件**跑完整链路：`useWorkbenchAnnotations` + 真 `KlineChart`
 * （canvas 层换成仓库既有的 lightweight-charts 替身）+ 真 `AnnotationLinkBar`，
 * 只把 `fetchAPI` 换成替身 —— 断言的是"图上此刻有几条线、点了几次、发了什么请求"。
 *
 * 覆盖：
 *  ① 必须用户**明确确认**才发撤销请求（只点「撤销」= 展开确认，不算确认）；
 *  ② 连点/重复调用只发一次 DELETE；成功后再次撤销不再发请求（那条线已不在图上）；
 *  ③ 失败（409/404）不清线：如实转述服务端原因 + 自动重新读取；
 *  ④ 成功 = 服务端读回后按目标重建，图上**只**少掉这一条；
 *  ⑤ 读回仍含该行（不同步）→ 保留线并明说"服务端仍返回"，不假装撤回；
 *  ⑥ 读回失败 → 只移除已确认撤销的那一条，其余不动并说明可重新读取；
 *  ⑦ 不属于本人的标注：本地就拦下，DELETE 一个都不发。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  /** DELETE 请求（顺序） */
  deletes: [] as string[],
  /** 存活价位线：handle → createPriceLine 配置 */
  live: new Map<object, Record<string, unknown>>(),
  /** `/copilot/annotations` 的响应（函数可返回 Promise） */
  annotations: (() => Promise.resolve([])) as (url: string) => Promise<unknown>,
  /** `DELETE /copilot/annotations/{id}` 的响应 */
  revoke: (() =>
    Promise.resolve({
      ok: true,
      status: 'revoked',
      annotation_id: 91,
      action_id: 7,
      action_status: 'rolled_back',
      symbol: '600111',
      label: '支撑位',
      reason: '',
      revoked_at: '2026-10-29T03:00:00Z',
    })) as (url: string) => Promise<unknown>,
}))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn((url: string, options?: { method?: string }) => {
    const u = String(url)
    H.calls.push(u)
    if (options?.method === 'DELETE' && u.startsWith('/copilot/annotations/')) {
      H.deletes.push(u)
      return H.revoke(u)
    }
    if (u.startsWith('/copilot/annotations')) return H.annotations(u)
    if (u.includes('/klines/')) {
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
import { requestAnnotations, revokeAnnotation } from '@/lib/copilot/annotationLink'
import type { AnnotationTarget, ServerAnnotationLine } from '@/lib/copilot/serverAnnotations'
import { useWorkbenchAnnotations } from '@/hooks/useWorkbenchAnnotations'

const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id }))}.signature`

const NOTE_TITLES = ['支撑位', '阻力位', '支撑 32.8', '阻力 36.5', '支撑', '阻力']
const noteLines = () =>
  [...H.live.values()].filter((l) => NOTE_TITLES.includes(String(l.title)))
const labels = () => noteLines().map((l) => `${String(l.title)}@${String(l.price)}`)

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

const ROW_SUPPORT = row({ id: 91, label: '支撑位', price: 32.8, action_id: 7 })
const ROW_PRESSURE = row({ id: 92, label: '阻力位', price: 36.5, color: 'down', action_id: 8 })

function revokedBody(overrides: Record<string, unknown> = {}) {
  return {
    ok: true,
    status: 'revoked',
    annotation_id: 91,
    action_id: 7,
    action_status: 'rolled_back',
    symbol: '600111',
    label: '支撑位',
    reason: '',
    revoked_at: '2026-10-29T03:00:00Z',
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

/** 页面等价接线：生产 hook + 真 KlineChart + 真状态条（撤销按钮接生产 `revokeAnnotation`）。 */
function Harness() {
  const t = target()
  const { snapshot, priceLines } = useWorkbenchAnnotations(t)
  return (
    <div>
      <KlineChart
        symbol="600111"
        market="CN"
        instrument="stock"
        initialInterval={'1d' as never}
        initialDays={120}
        height={360}
        supportPressure={priceLines}
      />
      <AnnotationLinkBar
        snapshot={snapshot}
        onRetry={() => {
          void requestAnnotations(t)
        }}
        onRevoke={(id) => {
          void revokeAnnotation(id)
        }}
      />
    </div>
  )
}

const annotationCalls = () => H.calls.filter((u) => u.startsWith('/copilot/annotations'))
const readCalls = () => annotationCalls().filter((u) => !H.deletes.includes(u))
const notice = () => screen.getByTestId('copilot-annotation-notice').textContent ?? ''

/** 展开并确认第一条线的撤销（用户的两次明确点击）。 */
function confirmFirstRevoke() {
  fireEvent.click(screen.getAllByTestId('copilot-annotation-revoke')[0])
  fireEvent.click(screen.getByTestId('copilot-annotation-revoke-yes'))
}

beforeEach(() => {
  localStorage.setItem('token', token('user-A'))
  resetAnnotationLedger()
  H.calls.length = 0
  H.deletes.length = 0
  H.live.clear()
  H.annotations = () => Promise.resolve([ROW_SUPPORT, ROW_PRESSURE])
  H.revoke = () => Promise.resolve(revokedBody())
})

afterEach(() => {
  cleanup()
  resetAnnotationLedger()
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('① 撤销必须经用户明确确认', () => {
  it('点「撤销」只是展开确认，不发 DELETE；取消则什么都不发生', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    fireEvent.click(screen.getAllByTestId('copilot-annotation-revoke')[0])
    const confirmBar = screen.getByTestId('copilot-annotation-revoke-confirm')
    expect(confirmBar.textContent).toContain('撤销标注 #91')
    expect(confirmBar.textContent).toContain('不可恢复')
    expect(H.deletes).toHaveLength(0)

    fireEvent.click(screen.getByTestId('copilot-annotation-revoke-cancel'))
    expect(screen.queryByTestId('copilot-annotation-revoke-confirm')).toBeNull()
    expect(H.deletes).toHaveLength(0)
    expect(noteLines()).toHaveLength(2)
  })
})

describe('② 成功后按服务端读回移除真实图层', () => {
  it('确认撤销 → DELETE 精确地址 → 读回后图上只少这一条', async () => {
    let rows: unknown[] = [ROW_SUPPORT, ROW_PRESSURE]
    H.annotations = () => Promise.resolve([...rows])
    H.revoke = () => {
      rows = rows.filter((r) => (r as { id: number }).id !== 91)
      return Promise.resolve(revokedBody())
    }

    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    confirmFirstRevoke()

    await waitFor(() => expect(H.deletes).toHaveLength(1))
    expect(H.deletes[0].startsWith('/copilot/annotations/91')).toBe(true)
    // 撤销之后必须重新读取（服务端是唯一事实源），图上才会真的少一条
    await waitFor(() => expect(labels()).toEqual(['阻力位@36.5']), { timeout: 3000 })
    await waitFor(() => expect(notice()).toContain('已撤销标注 #91'))
    expect(readCalls().length).toBeGreaterThanOrEqual(2)
  })

  it('连点只发一次 DELETE；成功后再次撤销不再发请求', async () => {
    let rows: unknown[] = [ROW_SUPPORT, ROW_PRESSURE]
    H.annotations = () => Promise.resolve([...rows])
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    let release: (value: unknown) => void = () => {}
    H.revoke = () => {
      rows = rows.filter((r) => (r as { id: number }).id !== 91)
      return new Promise((resolve) => {
        release = resolve
      })
    }

    // 同一目标、同一条标注的两个"并发"撤销调用（连点 / 双入口）
    const first = revokeAnnotation(91)
    const second = revokeAnnotation(91)
    expect(H.deletes).toHaveLength(1)

    release(revokedBody())
    await Promise.all([first, second])
    await waitFor(() => expect(H.deletes).toHaveLength(1))
    await waitFor(() => expect(labels()).toEqual(['阻力位@36.5']), { timeout: 3000 })

    // 撤销完成后那条线已不在账本里：再次撤销 = 本地拒绝，不发请求
    await revokeAnnotation(91)
    expect(H.deletes).toHaveLength(1)
    expect(notice()).toContain('不在当前图上')
  })

  it('目标不符（注入一条别的标的的线）时不发 DELETE', async () => {
    H.annotations = () => Promise.resolve([])
    render(<Harness />)
    await waitFor(() => expect(annotationCalls().length).toBeGreaterThanOrEqual(1))
    await new Promise((r) => setTimeout(r, 30))

    const forged: ServerAnnotationLine = {
      annotationId: 91,
      actionId: 7,
      price: 32.8,
      kind: 'support',
      label: '支撑位',
      symbol: '600519',
      market: 'CN',
      period: '1d',
      ownerId: 'user-A',
    }
    AnnotationLedger.replaceLines(target(), [forged], '')

    await waitFor(() => expect(screen.queryAllByTestId('copilot-annotation-revoke')).toHaveLength(1))
    confirmFirstRevoke()
    await waitFor(() => expect(notice()).toContain('标的'))
    expect(H.deletes).toHaveLength(0)
  })
})

describe('③ 失败：线保留 + 如实说明 + 自动重新读取', () => {
  it('DELETE 409 → 图上仍保留该线，说明服务端原因，并重新读取核对', async () => {
    H.annotations = () => Promise.resolve([ROW_SUPPORT, ROW_PRESSURE])
    H.revoke = () =>
      Promise.reject(
        Object.assign(new Error('标注 #91 对应的动作已是 rolled_back 状态（已撤销）'), {
          status: 409,
          kind: 'HTTP_4xx',
        }),
      )

    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })
    const readsBefore = readCalls().length

    confirmFirstRevoke()

    await waitFor(() => expect(H.deletes).toHaveLength(1))
    await waitFor(() => expect(notice()).toContain('409'))
    expect(notice()).toContain('已撤销')
    expect(notice()).toContain('仍保留')
    expect(noteLines()).toHaveLength(2)
    // 失败后按服务端重新读一遍，界面不靠本地猜测
    await waitFor(() => expect(readCalls().length).toBeGreaterThan(readsBefore))
    expect(notice()).toContain('已重新读取服务端标注核对')
  })

  it('撤销成功但读回仍包含该行 → 保留线并明说不同步', async () => {
    H.annotations = () => Promise.resolve([ROW_SUPPORT, ROW_PRESSURE])
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    confirmFirstRevoke()

    await waitFor(() => expect(notice()).toContain('仍返回'))
    expect(notice()).toContain('保留')
    expect(noteLines()).toHaveLength(2)
  })

  it('撤销成功但同价位还有另一条标注 → 保留下这个价位的线并说清是哪一条', async () => {
    // 连点确认过两次同一价位：图上一开始只画一条（id 大的那条），
    // 撤销它之后读回会返回同价位的另一条 —— 那不是"撤销失败"，也不是"没删掉"。
    let rows: unknown[] = [
      row({ id: 90, label: '支撑位', price: 32.8, action_id: 6 }),
      row({ id: 91, label: '支撑位', price: 32.8, action_id: 7 }),
      ROW_PRESSURE,
    ]
    H.annotations = () => Promise.resolve([...rows])
    H.revoke = () => {
      rows = rows.filter((r) => (r as { id: number }).id !== 91)
      return Promise.resolve(revokedBody())
    }

    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    confirmFirstRevoke()

    await waitFor(() => expect(H.deletes).toHaveLength(1))
    await waitFor(() => expect(notice()).toContain('同价位还有另一条标注 #90'))
    expect(labels()).toEqual(['支撑位@32.8', '阻力位@36.5'])
    // 被撤销的那条确实不在了
    expect(screen.getByTestId('copilot-annotation-ids').textContent).not.toContain('#91')
  })

  it('撤销成功但读回失败 → 只移除这一条，其余不动，并说明可重新读取', async () => {
    let first = true
    H.annotations = () => {
      if (first) {
        first = false
        return Promise.resolve([ROW_SUPPORT, ROW_PRESSURE])
      }
      return Promise.reject(new Error('读取服务端标注失败：network'))
    }

    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    confirmFirstRevoke()

    await waitFor(() => expect(H.deletes).toHaveLength(1))
    await waitFor(() => expect(labels()).toEqual(['阻力位@36.5']), { timeout: 3000 })
    await waitFor(() => expect(notice()).toContain('重新读取失败'))
    expect(notice()).toContain('只从图上移除了这一条')
    // 读回失败 ⇒ 给一个重新读取的入口，别让用户只能刷新整页
    expect(screen.getByTestId('copilot-annotation-retry')).toBeTruthy()
  })
})

describe('⑦ 不能删除不属于本人的标注', () => {
  it('账本里若出现他人标注，撤销在本地就被拦下，DELETE 一个都不发', async () => {
    H.annotations = () => Promise.resolve([])
    render(<Harness />)
    await waitFor(() => expect(annotationCalls().length).toBeGreaterThanOrEqual(1))
    await new Promise((r) => setTimeout(r, 30))

    const otherOwner: ServerAnnotationLine = {
      annotationId: 91,
      actionId: 7,
      price: 32.8,
      kind: 'support',
      label: '支撑位',
      symbol: '600111',
      market: 'CN',
      period: '1d',
      ownerId: 'user-B',
    }
    AnnotationLedger.replaceLines(target(), [otherOwner], '')

    await waitFor(() => expect(screen.queryAllByTestId('copilot-annotation-revoke')).toHaveLength(1))
    confirmFirstRevoke()
    await waitFor(() => expect(notice()).toContain('其他账号'))
    expect(H.deletes).toHaveLength(0)
  })
})
