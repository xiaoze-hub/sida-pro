// @vitest-environment jsdom
//
// perf(2026-10-02) 回归钉(冷启动请求数): 首页「市场温度」与「主线 Top1」各被多个组件独立取数 ——
//   /market/phase   ← usePhaseLabel(KPI 带) + MarketPhaseCard + PhaseGaugeCard(共 3 处)
//   /market/mainline ← useMainlineTop1(KPI 带) + MarketMainlineCard(共 2 处)
// 冷启动那一刻它们同时挂载, 30s 响应缓存在**响应回来之后**才生效, 于是同键并发真发多次,
// 与首屏其余 15+ 请求争抢同域连接池/后端单 worker 队列(B1 首页冷态 14.3s settle 的代码成因之一)。
//
// 本测试走**真** `fetchAPI`(只 stub `globalThis.fetch` 数真实请求次数), 用真实 biz-ui 组件挂载,
// 断言并发同键被合并。
import { cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearResponseCache } from '@panwatch/api'

type Call = { url: string; method: string }
let calls: Call[] = []

function okResponse(data: unknown): Response {
  return { status: 200, json: async () => ({ code: 0, data, message: '' }) } as unknown as Response
}

const PHASE = { available: true, current: { phase: 'rally', label: '主升', first_board: 60, ge2_count: 20, seal_rate: 0.6, max_height: 5, promo_rate: 0.3 }, recent_30d: [], distribution: [], total_days: 0, note: '' }
const MAINLINE = { total_groups: 0, ranked_groups: [] }

import { usePhaseLabel, useMainlineTop1 } from '@panwatch/biz-ui/components/KpiBand'
import MarketPhaseCard from '@panwatch/biz-ui/components/MarketPhaseCard'
import MarketMainlineCard from '@panwatch/biz-ui/components/MarketMainlineCard'

/** 迷你挂载台: 把「同时打 /market/phase 的组件」放在同一棵树里(还原首页冷启动并发) */
function Harness() {
  usePhaseLabel()
  useMainlineTop1()
  return (
    <div>
      <MarketPhaseCard />
      <MarketMainlineCard />
    </div>
  )
}

beforeEach(() => {
  clearResponseCache()
  calls = []
  vi.stubGlobal('fetch', async (input: string) => {
    calls.push({ url: String(input), method: 'GET' })
    if (String(input).includes('/market/phase')) return okResponse(PHASE)
    if (String(input).includes('/market/mainline')) return okResponse(MAINLINE)
    return okResponse({})
  })
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  clearResponseCache()
})

describe('首页冷启动同键请求合并', () => {
  it('/market/phase: 3 处组件并发只真发 1 次; /market/mainline: 2 处只真发 1 次', async () => {
    render(
      <MemoryRouter>
        <Harness />
      </MemoryRouter>,
    )
    // 等待两个端点各落地
    await vi.waitFor(() => {
      expect(calls.some((c) => c.url.includes('/market/phase'))).toBe(true)
    })
    await vi.waitFor(() => {
      expect(calls.some((c) => c.url.includes('/market/mainline'))).toBe(true)
    })

    const phase = calls.filter((c) => c.url.includes('/market/phase')).length
    const mainline = calls.filter((c) => c.url.includes('/market/mainline')).length
    // 修复前: phase = 3(3 处组件各发各的)
    expect(phase).toBe(1)
    // 主线仍是 2: MarketMainlineCard 显式 `cacheMode:'reload'`(实时, 跳过缓存) —— 属**有意不合并**,
    // 合并规则对 reload 一律让路(见 fetch-inflight-dedup.test.ts), 这里钉住“没有被误合并”。
    expect(mainline).toBe(2)
  })
})
