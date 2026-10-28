// @vitest-environment jsdom
/**
 * 端到端：**用户确认服务端情景标注 → 真实个股 K 线读取并显示**（2026-10-28 P2 三次收口）。
 *
 * 这条链路只有两跳是事实源，其余全是本地缓存/客户端状态：
 *
 *     POST /copilot/actions/{id}/apply   → 证明"哪个动作、状态 applied、payload 里的标的/价位/标签"
 *     GET  /copilot/annotations?symbol…  → 证明"服务端真的落了这条标注，价位是它说的那个"
 *
 * 本文件用**真实模块**（`lib/copilot/api` / `annotationLink` / `chartOverlayService` /
 * `serverAnnotations`）跑，只把 `fetchAPI` 换成替身 —— 网络之外的每一行都是生产代码。
 *
 * 覆盖（对应验收项）：
 *  a. 未确认不能上图：pending 只在 CopilotAction，读取接口读不到 ⇒ 一条不画；
 *  b. 已确认：价位/类型/标的逐字来自服务端行，事件 detail 带标注 id 与动作 id；
 *  c. 确认成功但读回对不上（周期/价位漂移）⇒ 不画 + 如实播报；
 *  d. 切账号 / 换标的 / 换周期无泄漏（id 信封按账号+标的+周期分键，服务端再过滤一层）；
 *  e. 撤销（服务端删行）后刷新不再显示，本地信封随之收敛；
 *  f. 缺数据 / 接口失败 / 响应形状不符 ⇒ 明确报错，不画、不猜。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@sida/api')>()
  return { ...actual, fetchAPI: mocks.fetchAPI }
})

import {
  applyPendingAction,
  getPendingActions,
  rollbackPendingAction,
} from '@/lib/copilot/api'
import {
  annotationReadyState,
  linkConfirmedAnnotation,
  restoreServerAnnotations,
} from '@/lib/copilot/annotationLink'
import {
  ANNOTATION_STATUS_EVENT,
  ChartOverlayService,
  OVERLAY_EVENT,
} from '@/lib/copilot/chartOverlayService'
import {
  annotationLinesFromRows,
  markServerVerified,
  parseAnnotationsResponse,
  SERVER_ANNOTATION_CONTRACT,
  type ServerAnnotationLine,
} from '@/lib/copilot/serverAnnotations'

const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id }))}.signature`
const REQ = { symbol: '600111', market: 'CN', period: '1d' }

/** 服务端 `KlineAnnotation` 行的真实形状（`get_copilot_annotations` 的返回字段）。 */
const ROW_SUPPORT = {
  id: 91,
  price: 32.8,
  ts: null,
  label: '支撑位',
  note: '情景矩阵支撑位（MA60/筹码带下沿/区间低）',
  color: 'up',
  source: 'ai',
  created_at: '2026-10-28 10:00:00',
}
const ROW_RESIST = {
  id: 92,
  price: 36.5,
  ts: null,
  label: '阻力位',
  note: '情景矩阵阻力位（MA20/筹码带上沿/区间高）',
  color: 'down',
  source: 'ai',
  created_at: '2026-10-28 10:00:00',
}

/** `POST /actions/{id}/apply` 的真实返回形状（`actions._to_dict`）。 */
const APPLIED_SUPPORT = {
  id: 7,
  run_id: null,
  kind: 'annotation',
  payload: {
    symbol: '600111',
    market: 'CN',
    label: '支撑位',
    price: 32.8,
    color: 'up',
    note: '情景矩阵支撑位（MA60/筹码带下沿/区间低）',
    source: 'scenario_matrix',
  },
  status: 'applied',
  result: '已标注 600111 @ 32.8：支撑位',
  diff: [],
  can_rollback: false,
  created_at: '2026-10-28 09:59:00',
  applied_at: '2026-10-28 10:00:00',
}

const keyFor = (userId: string, symbol = '600111', period = '1d') =>
  `sida_kline_annotation_ids:v1:${encodeURIComponent(userId)}:${encodeURIComponent(
    symbol,
  )}:${encodeURIComponent(period)}`

/** 走真实解析路径构造已标记的服务端价位线（等价于接线层从响应里拿到的那份）。 */
function serverLines(rows: unknown[], request = REQ): ServerAnnotationLine[] {
  const parsed = parseAnnotationsResponse(rows)
  if (!parsed.ok) throw new Error(parsed.message)
  return markServerVerified(annotationLinesFromRows(parsed.value.rows, request))
}

const isAnnotationsCall = (path: unknown) =>
  typeof path === 'string' && path.startsWith('/copilot/annotations')

const annotationsCalls = () =>
  mocks.fetchAPI.mock.calls.filter((call) => isAnnotationsCall(call[0]))

let overlayEvents: CustomEvent[] = []
let statusEvents: CustomEvent[] = []
const onOverlay = (e: Event) => overlayEvents.push(e as CustomEvent)
const onStatus = (e: Event) => statusEvents.push(e as CustomEvent)

/** 按路径分发的替身：`annotations` 返回标注行，`apply` 返回确认响应。 */
function routeFetch(options: {
  annotations?: unknown | (() => unknown)
  apply?: unknown
}) {
  mocks.fetchAPI.mockImplementation(async (path: string, opts?: { method?: string }) => {
    if (isAnnotationsCall(path)) {
      if (typeof options.annotations === 'function') return options.annotations()
      return options.annotations ?? []
    }
    if (typeof path === 'string' && path.endsWith('/apply')) {
      return options.apply ?? APPLIED_SUPPORT
    }
    if (typeof path === 'string' && path.includes('/rollback')) {
      // 与后端一致：`rollback_action` 对 kind="annotation" 直接 400
      throw new Error('annotation 动作不支持回滚')
    }
    if (path === '/copilot/actions') return []
    throw new Error(`未预料的请求：${path} ${opts?.method ?? 'GET'}`)
  })
}

beforeEach(() => {
  localStorage.setItem('token', token('user-A'))
  overlayEvents = []
  statusEvents = []
  mocks.fetchAPI.mockReset()
  window.addEventListener(OVERLAY_EVENT, onOverlay)
  window.addEventListener(ANNOTATION_STATUS_EVENT, onStatus)
})

afterEach(() => {
  window.removeEventListener(OVERLAY_EVENT, onOverlay)
  window.removeEventListener(ANNOTATION_STATUS_EVENT, onStatus)
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('a. 未确认不能上图', () => {
  it('pending 提议只在 /copilot/actions，读取接口读不到 → 一条价位线都不画', async () => {
    mocks.fetchAPI.mockImplementation(async (path: string) => {
      if (path === '/copilot/actions') {
        return [
          {
            id: 7,
            kind: 'annotation',
            payload: { symbol: '600111', label: '支撑位', price: 32.8 },
            status: 'pending',
            created_at: '2026-10-28 09:59:00',
          },
        ]
      }
      if (isAnnotationsCall(path)) return [] // 未 apply ⇒ KlineAnnotation 表里没有行
      throw new Error(`未预料的请求：${path}`)
    })

    const pending = await getPendingActions()
    expect(pending[0].status).toBe('pending')

    const state = await restoreServerAnnotations(REQ)

    expect(state.kind).toBe('idle')
    expect(overlayEvents).toHaveLength(0)
    expect(sessionStorage.getItem(keyFor('user-A'))).toBeNull()
  })

  it('服务端有标注行但本会话没有确认凭证 → 不画，并说明"无动作溯源/归属"（契约缺口）', async () => {
    routeFetch({ annotations: [ROW_SUPPORT, ROW_RESIST] })

    const state = await restoreServerAnnotations(REQ)

    expect(state.kind).toBe('unattributed')
    if (state.kind !== 'unattributed') throw new Error('unreachable')
    expect(state.serverRows).toBe(2)
    expect(state.message).toContain('未上图')
    expect(SERVER_ANNOTATION_CONTRACT.actionIdInRow).toBe(false)
    expect(SERVER_ANNOTATION_CONTRACT.ownerScopeInRow).toBe(false)
    expect(overlayEvents).toHaveLength(0)
  })
})

describe('b. 已确认 → 按服务端标注行上图（价位/类型/标的正确）', () => {
  it('apply 响应 + 读回行对得上才画，detail 带标注 id 与动作 id', async () => {
    routeFetch({ annotations: [ROW_SUPPORT, ROW_RESIST], apply: APPLIED_SUPPORT })

    await applyPendingAction(7)
    await vi.waitFor(() => expect(overlayEvents).toHaveLength(1))

    const detail = ChartOverlayService.readOverlayEventDetail(overlayEvents[0].detail)
    expect(detail?.symbol).toBe('600111')
    expect(detail?.period).toBe('1d')
    expect(
      detail?.lines.map((line) => ({
        id: line.annotationId,
        actionId: line.actionId,
        price: line.price,
        kind: line.kind,
        label: line.label,
        symbol: line.symbol,
      })),
    ).toEqual([
      { id: 91, actionId: 7, price: 32.8, kind: 'support', label: '支撑位', symbol: '600111' },
    ])

    // 会话存储只留标注 id（价位永远回服务端重读）
    const raw = sessionStorage.getItem(keyFor('user-A'))
    expect(raw).toBeTruthy()
    expect(JSON.parse(raw as string)).toEqual({
      symbol: '600111',
      period: '1d',
      annotationIds: [91],
    })
    expect(raw).not.toContain('32.8')

    // 读回必须绕过 fetchAPI 的 30s GET 缓存，否则刚确认的行读不到
    const reads = annotationsCalls()
    expect(reads).toHaveLength(1)
    expect(reads[0][1]).toMatchObject({ cacheMode: 'reload' })
    expect(reads[0][0]).toContain('symbol=600111')
    expect(reads[0][0]).toContain('market=CN')
    expect(reads[0][0]).toContain('period=1d')

    // 状态播报里同时给出标注 id 与动作 id
    const info = statusEvents.find((e) => e.detail.level === 'info')
    expect(info?.detail.message).toContain('#91')
    expect(info?.detail.message).toContain('#7')
  })

  it('刷新（重进页面）：用本会话确认过的标注 id 回服务端重读，服务端还在才画', async () => {
    routeFetch({ annotations: [ROW_SUPPORT, ROW_RESIST], apply: APPLIED_SUPPORT })
    await applyPendingAction(7)
    await vi.waitFor(() => expect(overlayEvents).toHaveLength(1))
    overlayEvents = []

    const state = await restoreServerAnnotations(REQ)

    expect(state.kind).toBe('ready')
    if (state.kind !== 'ready') throw new Error('unreachable')
    expect(state.annotationIds).toEqual([91])
    expect(state.actionIds).toEqual([]) // 刷新后没有 apply 响应 → 不编造 action id
    expect(overlayEvents).toHaveLength(1)
    expect(ChartOverlayService.readOverlayEventDetail(overlayEvents[0].detail)?.lines).toHaveLength(
      1,
    )
  })

  it('先后确认支撑与阻力：两条都留在信封里，刷新后一条不少', async () => {
    const appliedResist = {
      ...APPLIED_SUPPORT,
      id: 8,
      payload: {
        ...APPLIED_SUPPORT.payload,
        label: '阻力位',
        price: 36.5,
        color: 'down',
        note: '情景矩阵阻力位（MA20/筹码带上沿/区间高）',
      },
      result: '已标注 600111 @ 36.5：阻力位',
    }
    mocks.fetchAPI.mockImplementation(async (path: string) => {
      if (isAnnotationsCall(path)) return [ROW_SUPPORT, ROW_RESIST]
      if (path === '/copilot/actions/7/apply') return APPLIED_SUPPORT
      if (path === '/copilot/actions/8/apply') return appliedResist
      throw new Error(`未预料的请求：${path}`)
    })

    await applyPendingAction(7)
    await vi.waitFor(() => expect(annotationsCalls()).toHaveLength(1))
    await applyPendingAction(8)
    await vi.waitFor(() =>
      expect(
        JSON.parse(sessionStorage.getItem(keyFor('user-A')) as string).annotationIds,
      ).toEqual([91, 92]),
    )

    overlayEvents = []
    const state = await restoreServerAnnotations(REQ)
    expect(state.kind).toBe('ready')
    if (state.kind !== 'ready') throw new Error('unreachable')
    expect(state.lines.map((line) => [line.annotationId, line.price, line.kind])).toEqual([
      [91, 32.8, 'support'],
      [92, 36.5, 'pressure'],
    ])
  })

  it('加自选/盯盘提醒这类非标注动作不触发任何标注读取', async () => {
    routeFetch({
      annotations: [ROW_SUPPORT],
      apply: { ...APPLIED_SUPPORT, kind: 'watchlist', payload: { symbol: '600111' } },
    })

    await applyPendingAction(11)
    await Promise.resolve()

    expect(annotationsCalls()).toHaveLength(0)
    expect(overlayEvents).toHaveLength(0)
    expect(sessionStorage.getItem(keyFor('user-A'))).toBeNull()
  })
})

describe('c. 确认成功但读回对不上 → 不画，如实播报', () => {
  it('价位漂移（服务端行 33.10 ≠ 确认 32.80）不画线', async () => {
    routeFetch({
      annotations: [{ ...ROW_SUPPORT, price: 33.1 }],
      apply: APPLIED_SUPPORT,
    })

    await applyPendingAction(7)
    await vi.waitFor(() => expect(statusEvents.length).toBeGreaterThan(0))

    expect(overlayEvents).toHaveLength(0)
    expect(statusEvents.some((e) => e.detail.message.includes('找不到对应记录'))).toBe(true)
    expect(sessionStorage.getItem(keyFor('user-A'))).toBeNull()
  })

  it('确认响应不带 period → 按后端默认 1d 读回，不与页面当前周期混用', async () => {
    const appliedNoPeriod = { ...APPLIED_SUPPORT, payload: { ...APPLIED_SUPPORT.payload } }
    delete (appliedNoPeriod.payload as Record<string, unknown>).period
    routeFetch({ annotations: [ROW_SUPPORT], apply: appliedNoPeriod })

    await applyPendingAction(7)
    await vi.waitFor(() => expect(overlayEvents).toHaveLength(1))

    const reads = annotationsCalls()
    expect(reads).toHaveLength(1)
    expect(reads[0][0]).toContain('period=1d')
    // 信封落在 1d 上：页面在 5m 时不会误用它
    expect(sessionStorage.getItem(keyFor('user-A', '600111', '1d'))).toBeTruthy()
    expect(sessionStorage.getItem(keyFor('user-A', '600111', '5m'))).toBeNull()
  })

  it('动作状态不是 applied（例如已被回滚）→ 拒画并播报', async () => {
    routeFetch({
      annotations: [ROW_SUPPORT],
      apply: { ...APPLIED_SUPPORT, status: 'rolled_back' },
    })

    await applyPendingAction(7)
    await vi.waitFor(() => expect(statusEvents.length).toBeGreaterThan(0))

    expect(overlayEvents).toHaveLength(0)
    expect(statusEvents.some((e) => e.detail.message.includes('不是已应用'))).toBe(true)
  })
})

describe('d. 切账号 / 换标的 / 换周期无泄漏', () => {
  it('账号 B 读不到账号 A 的确认信封 → 即使服务端返回了行也不上图', async () => {
    routeFetch({ annotations: [ROW_SUPPORT, ROW_RESIST], apply: APPLIED_SUPPORT })
    await applyPendingAction(7)
    await vi.waitFor(() => expect(overlayEvents).toHaveLength(1))
    overlayEvents = []

    localStorage.setItem('token', token('user-B'))
    // 真实后端会按 user_id 过滤；这里故意返回同样的行，验证客户端不靠"服务端给了就是我的"
    const state = await restoreServerAnnotations(REQ)

    expect(state.kind).toBe('unattributed')
    expect(overlayEvents).toHaveLength(0)
    // A 的信封没被 B 动过
    expect(sessionStorage.getItem(keyFor('user-A'))).toBeTruthy()
    expect(sessionStorage.getItem(keyFor('user-B'))).toBeNull()
  })

  it('换标的：600111 的确认记录不会画到 000001 上', async () => {
    routeFetch({ annotations: [ROW_SUPPORT, ROW_RESIST], apply: APPLIED_SUPPORT })
    await applyPendingAction(7)
    await vi.waitFor(() => expect(overlayEvents).toHaveLength(1))
    overlayEvents = []

    const state = await restoreServerAnnotations({ ...REQ, symbol: '000001' })

    expect(state.kind).toBe('unattributed') // 000001 上有服务端行，但没有本会话确认凭证
    expect(overlayEvents).toHaveLength(0)
  })

  it('换周期：1d 的确认记录不会画到 5m 上', async () => {
    routeFetch({ annotations: [ROW_SUPPORT], apply: APPLIED_SUPPORT })
    await applyPendingAction(7)
    await vi.waitFor(() => expect(overlayEvents).toHaveLength(1))
    overlayEvents = []

    const state = await restoreServerAnnotations({ ...REQ, period: '5m' })

    expect(state.kind).toBe('unattributed')
    expect(overlayEvents).toHaveLength(0)
    expect(annotationsCalls().some((call) => String(call[0]).includes('period=5m'))).toBe(true)
  })
})

describe('e. 撤销 / 刷新', () => {
  it('服务端删掉一条 → 刷新后只画还在的，本地信封同步收敛', async () => {
    // 先建立"两条都确认过"的信封（等价于用户先确认了两次）
    const confirmed = ChartOverlayService.acceptServerLines(
      REQ.symbol,
      REQ.period,
      serverLines([ROW_SUPPORT, ROW_RESIST]),
    )
    expect(confirmed.saved).toBe(true)
    overlayEvents = []

    // 服务端只剩支撑位（阻力位被撤掉）
    routeFetch({ annotations: [ROW_SUPPORT] })
    const afterUndo = await restoreServerAnnotations(REQ)

    expect(afterUndo.kind).toBe('ready')
    if (afterUndo.kind !== 'ready') throw new Error('unreachable')
    expect(afterUndo.annotationIds).toEqual([91])
    expect(JSON.parse(sessionStorage.getItem(keyFor('user-A')) as string).annotationIds).toEqual([
      91,
    ])
  })

  it('服务端一条不剩 → 不画、信封清空，并如实说明"已不存在"', async () => {
    ChartOverlayService.acceptServerLines(REQ.symbol, REQ.period, serverLines([ROW_SUPPORT]))
    overlayEvents = []

    routeFetch({ annotations: [] })
    const state = await restoreServerAnnotations(REQ)

    expect(state.kind).toBe('unattributed')
    if (state.kind !== 'unattributed') throw new Error('unreachable')
    expect(state.message).toContain('已不存在')
    expect(overlayEvents).toHaveLength(0)
    expect(sessionStorage.getItem(keyFor('user-A'))).toBeNull()
  })

  it('撤销接口对 annotation 是 400：错误文案逐字转述，不假装撤回', async () => {
    routeFetch({ annotations: [ROW_SUPPORT], apply: APPLIED_SUPPORT })

    await expect(rollbackPendingAction(7)).rejects.toThrow('annotation 动作不支持回滚')
    expect(SERVER_ANNOTATION_CONTRACT.undoSupported).toBe(false)
  })
})

describe('f. 缺数据 / 接口失败 / 响应形状不符', () => {
  it('未登录：不发读取请求（避免 401 触发全局登出），也不画线', async () => {
    localStorage.removeItem('token')
    routeFetch({ annotations: [ROW_SUPPORT] })

    const state = await restoreServerAnnotations(REQ)

    expect(state.kind).toBe('idle')
    expect(annotationsCalls()).toHaveLength(0)
    expect(overlayEvents).toHaveLength(0)
  })

  it('读取失败 → error 且不画线', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('网络异常'))

    const state = await restoreServerAnnotations(REQ)

    expect(state.kind).toBe('error')
    if (state.kind !== 'error') throw new Error('unreachable')
    expect(state.message).toContain('读取服务端标注失败')
    expect(state.message).toContain('未画线')
    expect(overlayEvents).toHaveLength(0)
  })

  it('响应不是数组（契约不符）→ error，不把对象当空列表', async () => {
    routeFetch({ annotations: { rows: [ROW_SUPPORT] } })

    const state = await restoreServerAnnotations(REQ)

    expect(state.kind).toBe('error')
    if (state.kind !== 'error') throw new Error('unreachable')
    expect(state.message).toContain('不是列表')
  })

  it('确认后读取失败 → 播报"已确认但读取失败"，确认本身仍成功', async () => {
    let calls = 0
    mocks.fetchAPI.mockImplementation(async (path: string) => {
      if (typeof path === 'string' && path.endsWith('/apply')) return APPLIED_SUPPORT
      if (isAnnotationsCall(path)) {
        calls += 1
        throw new Error('超时')
      }
      throw new Error(`未预料的请求：${path}`)
    })

    const applied = await applyPendingAction(7)
    await vi.waitFor(() => expect(calls).toBe(1))
    await vi.waitFor(() => expect(statusEvents.length).toBeGreaterThan(0))

    expect(applied.status).toBe('applied') // 确认成功
    expect(overlayEvents).toHaveLength(0) // 但没画线
    expect(statusEvents.some((e) => e.detail.message.includes('读取服务端标注失败'))).toBe(true)
  })

  it('标注行全是脏数据 → 画不出任何线，也不产生事件', async () => {
    routeFetch({ annotations: [{ id: 0, price: 'x', label: '', source: 'robot' }] })

    const state = await restoreServerAnnotations(REQ)

    expect(state.kind).toBe('idle')
    expect(overlayEvents).toHaveLength(0)
  })
})

describe('g. 状态与价位线同源（供页面/状态带直接消费）', () => {
  it('annotationReadyState 的 id 全部来自行本身，不做补全', async () => {
    routeFetch({ annotations: [ROW_SUPPORT] })
    const state = annotationReadyState(serverLines([ROW_SUPPORT]))

    expect(state.kind).toBe('ready')
    if (state.kind !== 'ready') throw new Error('unreachable')
    expect(state.annotationIds).toEqual([91])
    expect(state.actionIds).toEqual([])
  })

  it('确认链路可独立调用（不依赖页面）：返回 ready 状态', async () => {
    routeFetch({ annotations: [ROW_SUPPORT] })
    const state = await linkConfirmedAnnotation(APPLIED_SUPPORT)

    expect(state?.kind).toBe('ready')
  })
})
