// @vitest-environment jsdom
/**
 * 标注撤销 —— 读回竞态与错误收口（2026-10-29 复审补丁）。
 *
 * 前一版（d772921a）在"单个撤销、无并发"下正确，但读回用**全量 GET + replaceLines**
 * 整批重建，且 replaceLines / beginRequest 会把 `revoking` 清空。复审指出的四个真实缺陷：
 *
 *  ① 两个不同 id 同时撤销 / 撤销与"新确认"同时发生：迟到的旧读回会把**已撤销的线复活**、
 *    把**刚确认的线抹掉**；
 *  ② `replaceLines` 清空 `revoking` → 另一条在途撤销的"撤销中…"消失、按钮重新可点（会发第二个
 *    DELETE，只剩 409）；
 *  ③ 撤销链路里 `return <promise>` 没有 await：`finally` 先跑（按钮提前解禁），接线层自己的异常
 *    直接变成**未处理拒绝**（逃出 catch）；
 *  ④ `fetchAPI` 的 401 是 `logout()` + 抛 `'登录已过期'`（**不带 status**），按 status 判会落进
 *    "网络异常"；确认条还写着"图上这条线立即移除"（事实是：删除成功并读回后才移除）。
 *
 * 另外：读回是"重建"而不是"对账"，全量行的 limit/新行都可能把**先前不在视图的线**带上来。
 *
 * 本文件用生产组件跑（`useWorkbenchAnnotations` + 真 KlineChart + 真状态条），只替身
 * `fetchAPI`；断言"图上此刻几条线、发了几个 DELETE、按钮是什么状态、提示说了什么"。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

/** 读回闸门：排一个 gate，下一次"全量读取"要等它释放才 resolve（模拟慢读回）。 */
type Gate = { promise: Promise<void>; release: () => void }

const H = vi.hoisted(() => ({
  calls: [] as string[],
  deletes: [] as string[],
  /** 存活价位线：handle → createPriceLine 配置 */
  live: new Map<object, Record<string, unknown>>(),
  /** 服务端此刻的标注行（读回按**请求发出那一刻**快照返回，才是真实的 GET 语义） */
  rows: [] as Record<string, unknown>[],
  gates: [] as Gate[],
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
    if (u.startsWith('/copilot/annotations')) {
      const q = new URL(u, 'http://local').searchParams
      const ids = (q.get('ids') ?? '')
        .split(',')
        .map((s) => Number(s))
        .filter((n) => Number.isFinite(n) && n > 0)
      const snap = H.rows
        .map((r) => ({ ...r }))
        .filter((r) => ids.length === 0 || ids.includes(Number(r.id)))
      const gate = H.gates.shift()
      return gate ? gate.promise.then(() => snap) : Promise.resolve(snap)
    }
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
    LineStyle: Record<string, number>
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

import { useState } from 'react'

import { AnnotationLinkBar } from '@/components/copilot/AnnotationLinkBar'
import { useWorkbenchAnnotations } from '@/hooks/useWorkbenchAnnotations'
import {
  AnnotationLedger,
  currentAccountId,
  resetAnnotationLedger,
  type AnnotationSnapshot,
} from '@/lib/copilot/annotationLedger'
import {
  linkConfirmedAnnotation,
  requestAnnotations,
  revokeAnnotation,
  setCurrentAnnotationTarget,
} from '@/lib/copilot/annotationLink'
import type { AnnotationTarget } from '@/lib/copilot/serverAnnotations'

const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id }))}.signature`

const ANNOTATION_TITLES = ['支撑位', '阻力位', '支撑 30', '阻力 40', '支撑 34']
const noteLines = () => [...H.live.values()].filter((l) => ANNOTATION_TITLES.includes(String(l.title)))
const labels = () =>
  noteLines().map((l) => `${String(l.title)}@${String(l.price)}`).sort()

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

/** 服务端真删了一行：DELETE 成功时调用，之后的读取就不再返回它。 */
function serverDeletes(id: number) {
  H.rows = H.rows.filter((r) => Number(r.id) !== id)
}

/** 排一个读回闸门（只影响下一次全量读取） */
function queueGate(): () => void {
  let release: () => void = () => {}
  const promise = new Promise<void>((resolve) => {
    release = resolve
  })
  H.gates.push({ promise, release })
  return release
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

/** 页面等价接线：真 hook + 真 KlineChart + 真状态条，视图可切换。 */
let switchView: (next: { symbol?: string; period?: string }) => void = () => {}

function Harness() {
  const [view, setView] = useState({ symbol: '600111', period: '1d' })
  switchView = (next) => setView((v) => ({ ...v, ...next }))
  const t = target({ symbol: view.symbol, period: view.period })
  const { snapshot, priceLines } = useWorkbenchAnnotations(t)
  return (
    <div>
      <KlineChart
        symbol={view.symbol}
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

const notice = () => screen.getByTestId('copilot-annotation-notice').textContent ?? ''
const spinners = () => screen.queryAllByTestId('copilot-annotation-revoking')
const revokeButtons = () => screen.queryAllByTestId('copilot-annotation-revoke')

/** 点「撤销」+「确认撤销」（用户的两次明确点击） */
function clickRevoke(index: number) {
  fireEvent.click(revokeButtons()[index])
  fireEvent.click(screen.getByTestId('copilot-annotation-revoke-yes'))
}

const tick = () => new Promise((r) => setTimeout(r, 30))

beforeEach(() => {
  localStorage.setItem('token', token('user-A'))
  resetAnnotationLedger()
  H.calls.length = 0
  H.deletes.length = 0
  H.gates.length = 0
  H.live.clear()
  H.rows = [ROW_SUPPORT, ROW_PRESSURE]
  H.revoke = () =>
    Promise.resolve(revokedBody())
})

afterEach(() => {
  cleanup()
  resetAnnotationLedger()
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('① 两个不同 id 并发撤销：互不覆盖', () => {
  it('另一条还在撤销中时，「撤销中…」不能被读回清掉', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    // #91：DELETE 立刻成功，但它的读回被闸住（慢读回）
    const releaseA = queueGate()
    let releaseB: () => void = () => {}
    H.revoke = (u) => {
      if (/\/91\?/.test(u)) {
        serverDeletes(91)
        return Promise.resolve(revokedBody())
      }
      // #92 的 DELETE 先挂住：这样"两条都在撤销中"是稳定状态
      return new Promise((resolve) => {
        releaseB = () => {
          serverDeletes(92)
          resolve(revokedBody({ annotation_id: 92, action_id: 8 }))
        }
      })
    }
    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(1))
    await waitFor(() => expect(spinners()).toHaveLength(1))

    // #92：另一个 id，同时撤销（#91 已是"撤销中…"，剩下的按钮就是 #92）
    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(2))
    await waitFor(() => expect(spinners()).toHaveLength(2))

    // 释放 #91 的读回（快照里还有 #92）—— 不能把 #92 的"撤销中…"清掉
    await act(async () => {
      releaseA()
      await tick()
    })
    expect(spinners()).toHaveLength(1)
    expect(spinners()[0].textContent).toContain('撤销中')

    await act(async () => {
      releaseB()
      await tick()
    })
    await waitFor(() => expect(labels()).toEqual([]), { timeout: 3000 })
    expect(spinners()).toHaveLength(0)
  })

  it('迟到的旧读回不能让已撤销的线复活', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    // #91 的读回慢（快照里 #92 还在），#92 的读回正常
    const releaseA = queueGate()
    H.revoke = (u) => {
      if (u.includes('/91')) {
        serverDeletes(91)
        return Promise.resolve(revokedBody())
      }
      serverDeletes(92)
      return Promise.resolve(revokedBody({ annotation_id: 92, action_id: 8 }))
    }

    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(1))
    clickRevoke(0) // #92（#91 还在"撤销中…"，#92 是剩下的那个按钮）
    // #92 先落地：#92 老实移除，#91 仍在（它自己的撤销还没读回）
    await waitFor(() => expect(labels()).toEqual(['支撑位@32.8']), { timeout: 3000 })

    // #91 的旧读回此刻才回来（它的快照里 #92 还在）
    await act(async () => {
      releaseA()
      await tick()
    })
    // 不许把已经被撤销的 #92 复活
    expect(labels()).toEqual([])
    expect(notice()).toContain('已撤销标注 #91')
  })
})

describe('② 撤销读回不能抹掉同屏新确认的标注', () => {
  it('读回在途时确认一条新标注，旧读回不许把它清掉', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    const releaseA = queueGate()
    H.revoke = () => {
      serverDeletes(91)
      return Promise.resolve(revokedBody())
    }
    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(1))

    // 期间用户在同一个视图上确认了一条新标注（服务端同步落库）
    H.rows = [...H.rows, row({ id: 93, label: '支撑 30', price: 30, action_id: 9 })]
    await act(async () => {
      await linkConfirmedAnnotation({
        id: 9,
        kind: 'annotation',
        status: 'applied',
        annotation_id: 93,
        annotation: { id: 93, symbol: '600111', market: 'CN', period: '1d', label: '支撑 30', price: 30 },
      })
    })
    // 此刻：#93 已上图，#91 还在（它的撤销读回还没回来），#92 也在
    expect(labels()).toEqual(['支撑 30@30', '支撑位@32.8', '阻力位@36.5'])

    // 旧读回（快照里只有 #92）落地：不许把 #93 抹掉，也不许把 #91 复活
    await act(async () => {
      releaseA()
      await tick()
    })
    expect(labels()).toEqual(['支撑 30@30', '阻力位@36.5'])
    expect(notice()).toContain('已撤销标注 #91')
  })
})

describe('③ 读回只对账被撤销的那条，不整批重建', () => {
  it('读回里出现先前不在视图的线（新行）也不会被带上来', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    // 服务端另有一行 #94（本条会话从没读过它，不在当前视图）
    H.rows = [...H.rows, row({ id: 94, label: '阻力 40', price: 40, color: 'down', action_id: 10 })]
    H.revoke = () => {
      serverDeletes(91)
      return Promise.resolve(revokedBody())
    }

    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(1))
    await waitFor(() => expect(notice()).toContain('已撤销标注 #91'), { timeout: 3000 })
    expect(labels()).toEqual(['阻力位@36.5'])
  })

  it('同价位还有另一条标注时，该价位仍有线（换的是另一条 id）', async () => {
    H.rows = [
      row({ id: 90, label: '支撑位', price: 32.8, action_id: 6 }),
      ROW_SUPPORT,
      ROW_PRESSURE,
    ]
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    H.revoke = () => {
      serverDeletes(91)
      return Promise.resolve(revokedBody())
    }
    clickRevoke(0)
    await waitFor(() => expect(notice()).toContain('同价位还有另一条标注 #90'), { timeout: 3000 })
    expect(labels()).toEqual(['支撑位@32.8', '阻力位@36.5'])
    expect(screen.getByTestId('copilot-annotation-ids').textContent).not.toContain('#91')
  })
})

describe('④ 读回在途时切换视图：旧图不改，新图不串线', () => {
  it('切周期', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    const releaseA = queueGate()
    H.revoke = () => {
      serverDeletes(91)
      return Promise.resolve(revokedBody())
    }
    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(1))

    // 切到 5m：服务端在该周期没有标注
    await act(async () => {
      switchView({ period: '5m' })
      await tick()
    })
    expect(labels()).toEqual([])

    await act(async () => {
      releaseA()
      await tick()
    })
    expect(labels()).toEqual([])
    expect(notice()).toContain('期间切换了图表视图')
    // 旧周期（1d）的账本不许被"5m 的读回"污染，新周期也不许出现旧线
    expect(AnnotationLedger.snapshot(target({ period: '5m' })).lines).toEqual([])
  })

  it('切标的', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    const releaseA = queueGate()
    H.revoke = () => {
      serverDeletes(91)
      return Promise.resolve(revokedBody())
    }
    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(1))

    H.rows = []
    await act(async () => {
      switchView({ symbol: '600519' })
      await tick()
    })
    expect(labels()).toEqual([])

    await act(async () => {
      releaseA()
      await tick()
    })
    expect(labels()).toEqual([])
    expect(notice()).toContain('期间切换了图表视图')
    expect(AnnotationLedger.snapshot(target({ symbol: '600519' })).lines).toEqual([])
  })

  it('切账号', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    const releaseA = queueGate()
    H.revoke = () => {
      serverDeletes(91)
      return Promise.resolve(revokedBody())
    }
    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(1))

    localStorage.setItem('token', token('user-B'))
    await act(async () => {
      setCurrentAnnotationTarget(target({ owner: 'user-B' }))
    })

    await act(async () => {
      releaseA()
      await tick()
    })
    // 旧账本的读回绝不写进新账号的键：新账号的图上不许出现别人（上一个账号）的线
    const other: AnnotationSnapshot = AnnotationLedger.snapshot(target({ owner: 'user-B' }))
    expect(other.lines).toEqual([])
    expect(labels()).toEqual([])
    // 也不许把这次读回的结果写回已经离开的那个视图（它下次进来会自己重新读）
    expect(AnnotationLedger.snapshot(target({ owner: 'user-A' })).lines).toHaveLength(2)
  })
})

describe('⑤ 接线层异常：收口成提示，不吞、也不变成未处理拒绝', () => {
  it('读回落地阶段抛错 → 播报接线异常，promise 仍 resolve，「撤销中…」解除', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    const boom = vi.spyOn(AnnotationLedger, 'replaceLines').mockImplementationOnce(() => {
      throw new Error('ledger 写入炸了')
    })
    H.revoke = () => {
      serverDeletes(91)
      return Promise.resolve(revokedBody())
    }

    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(1))
    await waitFor(() => expect(notice()).toContain('接线异常'), { timeout: 3000 })
    expect(notice()).toContain('ledger 写入炸了')
    expect(spinners()).toHaveLength(0)
    expect(boom).toHaveBeenCalledTimes(1)

    // 直接调用路径：不能 reject（未处理拒绝会让页面报错，也把"撤销成功"变成失败）
    boom.mockRestore()
    H.rows = [ROW_PRESSURE, row({ id: 95, label: '支撑 34', price: 34, action_id: 11 })]
    await act(async () => {
      await requestAnnotations(target())
      await tick()
    })
    await expect(revokeAnnotation(95)).resolves.toBeTruthy()
  })
})

describe('⑥ 失败文案按服务端语义说，不把 401 说成网络', () => {
  it('401（fetchAPI 只抛「登录已过期」，不带 status）→ 说登录失效', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    // fetchAPI 的 401 分支：logout() + throw new Error('登录已过期')，没有 status
    H.revoke = () => Promise.reject(new Error('登录已过期'))

    clickRevoke(0)
    await waitFor(() => expect(H.deletes).toHaveLength(1))
    await waitFor(() => expect(notice()).toContain('登录'), { timeout: 3000 })
    expect(notice()).not.toContain('网络异常')
    expect(noteLines()).toHaveLength(2)
  })

  it('超时（kind=TIMEOUT）→ 说超时/未确认删除，不冒充 5xx 或成功', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    H.revoke = () =>
      Promise.reject(Object.assign(new Error('请求超时（8000ms）'), { kind: 'TIMEOUT' }))

    clickRevoke(0)
    await waitFor(() => expect(notice()).toContain('超时'), { timeout: 3000 })
    expect(notice()).toContain('未确认')
    expect(noteLines()).toHaveLength(2)
  })
})

describe('⑦ 确认条文案与事实一致', () => {
  it('不承诺"立即移除"：删除成功并读回后才移除', async () => {
    render(<Harness />)
    await waitFor(() => expect(noteLines()).toHaveLength(2), { timeout: 3000 })

    fireEvent.click(revokeButtons()[0])
    const bar = screen.getByTestId('copilot-annotation-revoke-confirm')
    expect(bar.textContent).toContain('不可恢复')
    expect(bar.textContent).not.toContain('立即移除')
    expect(bar.textContent).toContain('读回')
  })
})
