// @vitest-environment jsdom
/**
 * 提案裁决并发回归（2026-10-22，代码审查 High）。
 *
 * 守四件事：
 *  ① 同一 tick 内连点两次「采纳」**只发一次 POST** —— 第二次直接返回 false。
 *     旧实现用 `decidingId`（React state）做重入锁，state 异步更新 → 双击穿透 → 两条裁决
 *     落库（stats 少两次），这是本文件存在的唯一理由；
 *  ② 裁决 resolve 之后锁必须释放（下一次裁决照常发出）；
 *  ③ 在途期间 `decidingId` 仍然等于该 id（行内禁用态靠它，不能为了「纯」删掉）；
 *  ④ `decide` 的引用在裁决前后**不变**（`decidingId` 不该在 useCallback 依赖里）。
 *
 * 网络全部打桩（stubGlobal fetch），POST /decide 用**手动 resolve 的 pending Promise**
 * 卡住，才能复现「请求还在途时的第二次调用」。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'

import { useCopilotProposals } from '@/hooks/useCopilotProposals'

const ROW = {
  id: 1,
  dimension: '北向资金日内节奏',
  reason: '配方只有日频净流入，看不到日内节奏',
  evidence: '',
  symbol: '600000',
  recipe: 'capital_flow',
  status: 'pending',
  seen_count: 1,
  first_seen_at: '2026-10-21 10:00:00',
  last_seen_at: '2026-10-22 10:00:00',
}

interface DecideCall {
  id: number
  action: string
}

/** 已发出的裁决 POST（顺序即真实发出顺序） */
let decideCalls: DecideCall[] = []
/** 在途裁决的闸门：测试自己决定什么时候回包 */
let gates: Array<{ id: number; resolve: () => void }> = []

function envelope(data: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({ code: 0, success: true, data, message: 'ok' }),
  } as unknown as Response
}

beforeEach(() => {
  decideCalls = []
  gates = []

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL, init?: RequestInit): Promise<Response> => {
      const href = String(url)
      const method = (init?.method ?? 'GET').toUpperCase()

      // 裁决：POST 必须最先判，否则被 /copilot/proposals 前缀吃掉
      const decideMatch = /\/copilot\/proposals\/(\d+)\/decide(?:[?#]|$)/.exec(href)
      if (method === 'POST' && decideMatch) {
        const id = Number(decideMatch[1])
        const action = JSON.parse(String(init?.body ?? '{}')).action as string
        decideCalls.push({ id, action })

        // 手动控制的在途 Promise：不 resolve 就永远不回包
        let open!: (res: Response) => void
        const gate = new Promise<Response>((resolve) => {
          open = resolve
        })
        gates.push({
          id,
          resolve: () => open(envelope({ ok: true, proposal: { ...ROW, id, status: action } })),
        })
        return gate
      }

      if (href.includes('/copilot/proposals/stats')) {
        return envelope({
          items: [],
          top: null,
          summary: { total: 1, pending: 1, accept: 0, reject: 0, snooze: 0 },
        })
      }

      if (href.includes('/copilot/proposals')) {
        return envelope([{ ...ROW }])
      }

      return envelope([])
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

/** 挂载 hook 并等首屏取数结束（列表已上屏）。 */
async function mount() {
  const view = renderHook(() => useCopilotProposals('pending'))
  await waitFor(() => expect(view.result.current.loading).toBe(false))
  expect(view.result.current.rows).toHaveLength(1)
  return view
}

describe('useCopilotProposals.decide 并发保护', () => {
  it('同一 tick 连点两次：只发一次 POST，第二次返回 false', async () => {
    const { result } = await mount()

    let first!: Promise<boolean>
    let second!: Promise<boolean>
    await act(async () => {
      // 同一事件循环内连点：闭包里读到的 state 都还是旧值，正是穿透的那一次
      first = result.current.decide(1, 'accept')
      second = result.current.decide(1, 'accept')
    })

    // 核心断言：后端只能收到一条裁决
    expect(decideCalls).toHaveLength(1)
    expect(decideCalls[0]).toEqual({ id: 1, action: 'accept' })
    // 行内禁用态仍靠 decidingId（不是把它删掉换来的「纯」）
    expect(result.current.decidingId).toBe(1)

    await act(async () => {
      gates[0].resolve()
      await first
    })
    expect(await first).toBe(true)
    expect(await second).toBe(false)
  })

  it('在途期间换一条提案裁决也被挡住（同一时刻只允许一条在途）', async () => {
    const { result } = await mount()

    let first!: Promise<boolean>
    let other!: Promise<boolean>
    await act(async () => {
      first = result.current.decide(1, 'accept')
      other = result.current.decide(2, 'reject')
    })

    expect(decideCalls).toHaveLength(1)
    expect(await other).toBe(false)

    await act(async () => {
      gates[0].resolve()
      await first
    })
    expect(await first).toBe(true)
  })

  it('裁决 resolve 后锁释放：可以再次裁决', async () => {
    const { result } = await mount()

    let first!: Promise<boolean>
    await act(async () => {
      first = result.current.decide(1, 'accept')
    })
    await act(async () => {
      gates[0].resolve()
      await first
    })
    expect(await first).toBe(true)
    await waitFor(() => expect(result.current.decidingId).toBeNull())

    let again!: Promise<boolean>
    await act(async () => {
      again = result.current.decide(1, 'reject')
    })
    // 锁真放开了：第二条 POST 发出去了
    expect(decideCalls).toHaveLength(2)
    expect(decideCalls[1]).toEqual({ id: 1, action: 'reject' })

    await act(async () => {
      gates[1].resolve()
      await again
    })
    expect(await again).toBe(true)
  })

  it('裁决期间 decide 引用不变（decidingId 不进 useCallback 依赖）', async () => {
    const { result } = await mount()
    const before = result.current.decide

    let first!: Promise<boolean>
    await act(async () => {
      first = result.current.decide(1, 'accept')
    })
    // decidingId 变了、组件重渲染了，但函数引用必须还是同一个
    expect(result.current.decidingId).toBe(1)
    expect(result.current.decide).toBe(before)

    await act(async () => {
      gates[0].resolve()
      await first
    })
  })
})
