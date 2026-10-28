// @vitest-environment jsdom
/**
 * 标注账号/标的/周期隔离（2026-10-28 P2 四收口重写）。
 *
 * 旧实现把"我确认过哪些标注 id"写进 sessionStorage 信封，再用 WeakSet 给数组"盖章"。
 * 复审驳回：两者都是客户端自证，且信封跨账号靠拼 key、跨标的靠拼 key —— 一旦拼错就串线。
 *
 * 现在隔离由**服务端回显 + 账本按精确目标分键**共同保证：
 *  - 服务端只回本人行（`mine_only=1`），并回显 `user_id`；全局行/他人行核对必拒；
 *  - 账本 key = `账号|标的类型|代码|市场|周期`，换任何一项都是另一份快照；
 *  - 会话存储不再参与（本文件断言 sessionStorage 始终为空）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import {
  AnnotationLedger,
  currentAccountId,
  resetAnnotationLedger,
} from '@/lib/copilot/annotationLedger'
import { requestAnnotations } from '@/lib/copilot/annotationLink'
import type { AnnotationTarget } from '@/lib/copilot/serverAnnotations'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@sida/api')>()
  return { ...actual, fetchAPI: mocks.fetchAPI }
})

const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id }))}.signature`

const target = (overrides: Partial<AnnotationTarget> = {}): AnnotationTarget => ({
  owner: currentAccountId() ?? '',
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
    note: null,
    color: 'up',
    source: 'ai',
    created_at: '2026-10-28 10:00:00',
    ...overrides,
  }
}

beforeEach(() => {
  localStorage.setItem('token', token('user-A'))
  resetAnnotationLedger()
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockImplementation(async () => [row()])
})

afterEach(() => {
  cleanup()
  resetAnnotationLedger()
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('画线账号/标的/周期隔离（服务端身份 + 目标分键）', () => {
  it('换账号：同一标的同一条服务端行，对另一个账号是"他人标注"，不上图', async () => {
    const a = await requestAnnotations(target())
    expect(a.lines.map((l) => l.annotationId)).toEqual([91])

    localStorage.setItem('token', token('user-B'))
    // 服务端（替身）仍返回 user-A 的行 —— 属主核对必须拒掉
    const b = await requestAnnotations(target({ owner: 'user-B' }))
    expect(b.lines).toHaveLength(0)
    expect(b.phase).toBe('refused')
    expect(b.message).toContain('其他账号')
    // A 的快照不受影响（各 key 独立），但 B 读不到 A 的线
    expect(AnnotationLedger.snapshot(target({ owner: 'user-A' })).lines).toHaveLength(1)
    expect(AnnotationLedger.snapshot(target({ owner: 'user-B' })).lines).toHaveLength(0)
  })

  it('换标的：同账号下 600111 的标注不会出现在 002636 的快照里', async () => {
    await requestAnnotations(target())
    const other = await requestAnnotations(target({ symbol: '002636' }))
    expect(other.lines).toHaveLength(0)
    expect(other.phase).toBe('refused') // 行 symbol 不符 → 明确拒，不是静默空
    expect(other.message).toContain('属于 600111')
    expect(other.message).toContain('002636')
  })

  it('换周期：1d 的线不会出现在 5m 快照里', async () => {
    await requestAnnotations(target())
    const five = await requestAnnotations(target({ period: '5m' }))
    expect(five.lines).toHaveLength(0)
    expect(five.message).toContain('周期')
  })

  it('个股↔指数：指数目标与个股目标互不共享快照', async () => {
    await requestAnnotations(target())
    const index = await requestAnnotations(target({ instrument: 'index', symbol: '000001' }))
    expect(index.phase).toBe('refused')
    expect(index.lines).toHaveLength(0)
    expect(AnnotationLedger.snapshot(target()).lines).toHaveLength(1)
  })

  it('未登录：不发请求、不写任何存储（会话存储不再参与画线）', async () => {
    localStorage.removeItem('token')
    const snap = await requestAnnotations(target({ owner: '' }))
    expect(snap.phase).toBe('refused')
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
    expect(sessionStorage.length).toBe(0)
  })

  it('整条链路不写 sessionStorage（旧 id 信封已删除）', async () => {
    await requestAnnotations(target())
    expect(sessionStorage.length).toBe(0)
  })
})

describe('AnswerCard 不提供本地画线入口', () => {
  it('正文价位只作文本/锚点：没有"在 K 线叠加画线"，不广播叠加事件', () => {
    const overlaySpy = vi.fn()
    window.addEventListener('sida-kline-overlay', overlaySpy)
    const parsed = parseAnswer(
      '【结论】观察\n【依据】\n- 支撑位 32.80 元\n- 阻力位 36.50 元\n【预案】观望\n【口径】行情',
    )
    render(<AnswerCard parsed={parsed} symbol="600111" />)

    expect(screen.queryByText('在 K 线叠加画线')).toBeNull()
    expect(screen.getByTestId('copilot-level-provenance-note')).toBeTruthy()
    expect(overlaySpy).not.toHaveBeenCalled()
    window.removeEventListener('sida-kline-overlay', overlaySpy)
  })
})
