// @vitest-environment jsdom
/**
 * 端到端：**用户确认服务端情景标注 → 真 K 线上的价位线**（2026-10-28 P2 四收口重写）。
 *
 * 这条链路只有两跳是事实源：
 *
 *     POST /copilot/actions/{id}/apply
 *        → 证明"哪个动作、状态 applied、落库的是哪一条标注（annotation_id）、明细是什么"
 *     GET  /copilot/annotations?...&ids=<annotation_id>&market_strict=1&mine_only=1&action_linked=1
 *        → 证明"这一条现在还在服务端、属主是我、标的/市场/周期与当前 K 线一致"
 *
 * 本文件用**真实模块**（`lib/copilot/api` / `annotationLink` / `annotationLedger` /
 * `serverAnnotations`）跑，只把 `fetchAPI` 换成替身 —— 网络之外的每一行都是生产代码。
 * 真实图表探针见 `tests/components/copilot-annotation-channel.test.tsx`。
 *
 * 覆盖：
 *  a. 未确认不能上图：pending 只在 CopilotAction，读取接口读不到 ⇒ 一条不画；
 *  b. 已确认：apply 响应给 annotation_id，精确回读后才画，id/动作 id 都可溯源；
 *  c. 读取失败 / 没回读到这一条 ⇒ 不画 + 如实播报；
 *  d. 跨标的 / 换周期 ⇒ 不画，并说清"落在哪个标的/周期"（不谎称已上图）；
 *  e. 跨账号确认（服务端 400）⇒ 确认本身失败，图上一条不加；
 *  f. 撤销不可用：rollback 的 400 原样透传，不假装能撤。
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
import { AnnotationLedger, resetAnnotationLedger } from '@/lib/copilot/annotationLedger'
import {
  linkConfirmedAnnotation,
  requestAnnotations,
  setCurrentAnnotationTarget,
} from '@/lib/copilot/annotationLink'
import {
  SERVER_ANNOTATION_CONTRACT,
  type AnnotationTarget,
} from '@/lib/copilot/serverAnnotations'

const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id }))}.signature`

const target = (overrides: Partial<AnnotationTarget> = {}): AnnotationTarget => ({
  owner: 'user-A',
  instrument: 'stock',
  symbol: '600111',
  market: 'CN',
  period: '1d',
  ...overrides,
})

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
    note: '情景矩阵支撑位（MA60/筹码带下沿/区间低）',
    color: 'up',
    source: 'ai',
    created_at: '2026-10-28 10:00:00',
    ...overrides,
  }
}

/** `POST /actions/{id}/apply` 的新契约返回（`actions.apply_action`）。 */
function appliedBody(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    run_id: null,
    kind: 'annotation',
    payload: { symbol: '600111', market: 'CN', label: '支撑位', price: 32.8, source: 'scenario_matrix' },
    status: 'applied',
    result: '已标注 600111 @ 32.8：支撑位',
    annotation_id: 91,
    annotation: {
      id: 91,
      symbol: '600111',
      market: 'CN',
      period: '1d',
      price: 32.8,
      ts: null,
      label: '支撑位',
      color: 'up',
      source: 'ai',
    },
    diff: [],
    can_rollback: false,
    created_at: '2026-10-28 09:59:00',
    applied_at: '2026-10-28 10:00:00',
    ...overrides,
  }
}

const annotationCalls = () =>
  mocks.fetchAPI.mock.calls.filter((c) => String(c[0]).startsWith('/copilot/annotations'))

beforeEach(() => {
  localStorage.setItem('token', token('user-A'))
  resetAnnotationLedger()
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockImplementation(async () => [])
})

afterEach(() => {
  resetAnnotationLedger()
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('a. 未确认不能上图', () => {
  it('pending 提议只在 /copilot/actions，读取接口读不到 ⇒ 一条价位线都没有', async () => {
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
      if (String(path).startsWith('/copilot/annotations')) return []
      throw new Error(`未预料的请求：${path}`)
    })

    const pending = await getPendingActions()
    expect(pending[0].status).toBe('pending')

    setCurrentAnnotationTarget(target())
    const snap = await requestAnnotations(target())

    expect(snap.phase).toBe('idle')
    expect(snap.lines).toHaveLength(0)
    expect(sessionStorage.length).toBe(0)
  })
})

describe('b. 已确认：apply 响应的 annotation_id → 精确回读 → 上图', () => {
  it('沙箱链路：回读返回同一条 → 画出服务端价位，id/动作 id 可溯源', async () => {
    setCurrentAnnotationTarget(target())
    await requestAnnotations(target()) // 进页面：此时还没有标注
    expect(AnnotationLedger.snapshot(target()).lines).toHaveLength(0)

    // 确认后服务端才有这一行
    mocks.fetchAPI.mockImplementation(async (path: string) => {
      if (String(path).includes('/apply')) return appliedBody()
      if (String(path).startsWith('/copilot/annotations')) return [row()]
      throw new Error(`未预料的请求：${path}`)
    })

    const snap = await linkConfirmedAnnotation(appliedBody())
    expect(snap?.phase).toBe('ready')
    expect(snap?.lines.map((l) => [l.annotationId, l.actionId, l.price])).toEqual([[91, 7, 32.8]])
    expect(snap?.annotationIds).toEqual([91])
    expect(snap?.actionIds).toEqual([7])
    expect(snap?.notice).toContain('已确认并上图')

    const exactCalls = annotationCalls().filter((c) => String(c[0]).includes('ids=91'))
    expect(exactCalls.length).toBeGreaterThan(0)
    const exact = String(exactCalls[exactCalls.length - 1][0])
    expect(exact).toContain('ids=91')
    expect(exact).toContain('market_strict=1')
    expect(exact).toContain('mine_only=1')
    expect(exact).toContain('action_linked=1')
  })

  it('走生产的 applyPendingAction：确认成功后接线自动跑，账本出现同一 id', async () => {
    setCurrentAnnotationTarget(target())
    mocks.fetchAPI.mockImplementation(async (path: string) => {
      if (String(path).includes('/apply')) return appliedBody()
      if (String(path).startsWith('/copilot/annotations')) return [row()]
      return []
    })

    const applied = await applyPendingAction(7)
    expect(applied.status).toBe('applied')

    // 接线是 void 发起（不阻塞确认返回）—— 等一拍再断言
    await vi.waitFor(() => {
      expect(AnnotationLedger.snapshot(target()).annotationIds).toEqual([91])
    })
    expect(AnnotationLedger.snapshot(target()).lines[0].actionId).toBe(7)
  })
})

describe('c. 读不回来 ⇒ 不画，且如实说', () => {
  it('精确回读为空（撤销/事务未落地）→ 不画，播报里带标注 id', async () => {
    setCurrentAnnotationTarget(target())
    mocks.fetchAPI.mockImplementation(async () => [])
    const snap = await linkConfirmedAnnotation(appliedBody())
    expect(snap?.lines).toHaveLength(0)
    expect(snap?.notice).toContain('#91')
    expect(snap?.notice).toContain('未画线')
  })

  it('读取接口失败 → 不画，播报说清失败原因', async () => {
    setCurrentAnnotationTarget(target())
    mocks.fetchAPI.mockImplementation(async (path: string) => {
      if (String(path).startsWith('/copilot/annotations')) throw new Error('网络异常')
      return []
    })
    const snap = await linkConfirmedAnnotation(appliedBody())
    expect(snap?.lines).toHaveLength(0)
    expect(snap?.notice).toContain('读取服务端标注失败')
    expect(snap?.noticeLevel).toBe('error')
  })
})

describe('d. 跨标的/跨周期：不画，并说清落在哪', () => {
  it('当前看 002636，确认的是 600111 的标注 → 未上图并指名两个代码', async () => {
    setCurrentAnnotationTarget(target({ symbol: '002636' }))
    mocks.fetchAPI.mockImplementation(async () => [row()])
    const snap = await linkConfirmedAnnotation(appliedBody())
    expect(snap?.lines).toHaveLength(0)
    expect(snap?.notice).toContain('600111')
    expect(snap?.notice).toContain('002636')
    expect(snap?.notice).toContain('未上图')
    expect(snap?.notice).not.toContain('已确认并上图')
  })

  it('当前看 5m，确认落库在 1d → 未上图并说明周期（后端 propose 不带视图周期）', async () => {
    setCurrentAnnotationTarget(target({ period: '5m' }))
    mocks.fetchAPI.mockImplementation(async () => [row({ period: '5m' })])
    const snap = await linkConfirmedAnnotation(appliedBody())
    expect(snap?.lines).toHaveLength(0)
    expect(snap?.notice).toContain('落库在 1d')
    expect(snap?.notice).toContain('5m')
    expect(snap?.notice).toContain('未上图')
    // 5m 自己的快照不因这次确认而变
    expect(AnnotationLedger.snapshot(target({ period: '5m' })).lines).toHaveLength(0)
  })
})

describe('e. 跨账号确认：服务端 400 ⇒ 确认本身失败，图上一条不加', () => {
  it('apply 返回 400「动作不存在」→ applyPendingAction 抛错，账本不动', async () => {
    setCurrentAnnotationTarget(target())
    mocks.fetchAPI.mockImplementation(async (path: string) => {
      if (String(path).includes('/apply')) throw new Error('动作不存在')
      return []
    })

    await expect(applyPendingAction(999)).rejects.toThrow('动作不存在')
    expect(AnnotationLedger.snapshot(target()).lines).toHaveLength(0)
    expect(annotationCalls()).toHaveLength(0) // 没有精确回读，更没有画线
  })

  it('apply 响应说 applied，但回读行属主是别人 → 不画', async () => {
    setCurrentAnnotationTarget(target())
    mocks.fetchAPI.mockImplementation(async () => [row({ user_id: 'user-B' })])
    const snap = await linkConfirmedAnnotation(appliedBody())
    expect(snap?.lines).toHaveLength(0)
    expect(snap?.notice).toContain('其他账号')
  })
})

describe('f. 撤销不可用（契约事实，不假装能撤）', () => {
  it('rollback annotation 的 400 原样透传；契约里 undoSupported=false', async () => {
    mocks.fetchAPI.mockImplementation(async (path: string) => {
      if (String(path).includes('/rollback')) throw new Error('annotation 动作不支持回滚')
      return []
    })
    await expect(rollbackPendingAction(7)).rejects.toThrow('annotation 动作不支持回滚')
    expect(SERVER_ANNOTATION_CONTRACT.undoSupported).toBe(false)
  })
})
