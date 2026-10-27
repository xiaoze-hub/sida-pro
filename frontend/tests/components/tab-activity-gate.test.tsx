// @vitest-environment jsdom
/**
 * 页签可见性 → 共享数据层停表 的钉子 —— 2026-10-15。
 *
 * keep-alive 的代价是「N 页签 = N 路数据流」, 所以我们把闸门装在**共享数据层**
 * (轮询 hook / react-query 薄壳 / WS 管理层), 而不是逐页去改。本文件守三件事:
 *   ① `useTabActive()`: 未包 Provider → 视作可见(true); 藏起来 → false;
 *   ② `usePolling`: 隐藏时**不打表也不首拉**, 回到可见**立即拉一次**并恢复定时;
 *   ③ `useApiQuery`: 隐藏时**掐掉 refetchInterval**(初次取数照常), 回到可见立即补一次;
 *   ④ `useQuoteStream`: 隐藏时**不建 WS 连接**, 回到可见才连。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn(), getToken: vi.fn(() => 'tok') }))

vi.mock('@sida/api', () => ({
  fetchAPI: (...args: unknown[]) => api.fetchAPI(...args),
  getToken: () => api.getToken(),
}))

import { TabActiveProvider, useTabActive } from '@sida/base-ui'
import { usePolling } from '@sida/biz-ui/hooks/usePolling'

import { useApiQuery } from '@/hooks/useApiQuery'
import { useQuoteStream } from '@/realtime/useQuoteStream'

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

let pollCalls = 0
/** 稳定引用的 loader(否则 usePolling 的 effect 每渲染重跑, 计数会飘) */
const stableLoader = async () => {
  pollCalls += 1
  return pollCalls
}

function ActiveProbe() {
  return <div data-testid="active-flag">{String(useTabActive())}</div>
}

function PollProbe() {
  const { data } = usePolling({ loader: stableLoader, intervalMs: 40, immediate: true })
  return <div data-testid="poll-data">{String(data ?? 'none')}</div>
}

function QueryProbe() {
  const { data } = useApiQuery<{ ok: number }>(['gate-key'], '/gate', { refetchInterval: 40 })
  return <div data-testid="query-data">{String(data?.ok ?? 'none')}</div>
}

function StreamProbe() {
  const status = useQuoteStream(() => {}, true)
  return <div data-testid="stream-status">{status}</div>
}

let wsCreated = 0
class FakeWebSocket {
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((ev: { code: number }) => void) | null = null
  constructor(_url: string, _protocols?: string[]) {
    wsCreated += 1
  }
  close() {
    /* noop */
  }
}

beforeEach(() => {
  pollCalls = 0
  wsCreated = 0
  api.fetchAPI.mockReset()
  api.getToken.mockReset()
  api.getToken.mockReturnValue('tok')
  api.fetchAPI.mockResolvedValue({ ok: 1 })
  vi.stubGlobal('WebSocket', FakeWebSocket)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('① useTabActive: 默认可见, Provider 说了算', () => {
  it('未包 Provider → true(壳层 chrome / 测试里的散件照常工作)', () => {
    render(<ActiveProbe />)
    expect(screen.getByTestId('active-flag').textContent).toBe('true')
  })

  it('TabActiveProvider active={false} → false', () => {
    render(
      <TabActiveProvider active={false}>
        <ActiveProbe />
      </TabActiveProvider>,
    )
    expect(screen.getByTestId('active-flag').textContent).toBe('false')
  })
})

describe('② usePolling: 隐藏停表, 回到可见立即补一次', () => {
  it('隐藏 → 不首拉、不打表; 切到可见 → 立即拉一次并恢复轮询', async () => {
    const { rerender } = render(
      <TabActiveProvider active={false}>
        <PollProbe />
      </TabActiveProvider>,
    )
    await sleep(150)
    expect(pollCalls).toBe(0) // 隐藏: 一次都没拉

    rerender(
      <TabActiveProvider active>
        <PollProbe />
      </TabActiveProvider>,
    )
    await waitFor(() => expect(pollCalls).toBeGreaterThanOrEqual(1))
    const afterActivate = pollCalls
    await waitFor(() => expect(pollCalls).toBeGreaterThan(afterActivate), { timeout: 2000 }) // 定时恢复
  })
})

describe('③ useApiQuery: 隐藏停轮询, 回到可见立即补一次', () => {
  it('隐藏 → 初次取数照常但不轮询; 切到可见 → 立即补一次', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
    })
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <TabActiveProvider active={false}>
          <QueryProbe />
        </TabActiveProvider>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(api.fetchAPI).toHaveBeenCalledTimes(1)) // 初次取数不受影响
    await sleep(150)
    expect(api.fetchAPI).toHaveBeenCalledTimes(1) // 隐藏: 不轮询

    rerender(
      <QueryClientProvider client={client}>
        <TabActiveProvider active>
          <QueryProbe />
        </TabActiveProvider>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(api.fetchAPI.mock.calls.length).toBeGreaterThan(1))
  })
})

describe('④ useQuoteStream: 隐藏不建 WS, 回到可见才连', () => {
  it('隐藏 → 不建连接(status=offline); 可见 → 建连接', () => {
    const { rerender } = render(
      <TabActiveProvider active={false}>
        <StreamProbe />
      </TabActiveProvider>,
    )
    expect(wsCreated).toBe(0)
    expect(screen.getByTestId('stream-status').textContent).toBe('offline')

    rerender(
      <TabActiveProvider active>
        <StreamProbe />
      </TabActiveProvider>,
    )
    expect(wsCreated).toBe(1)
    expect(screen.getByTestId('stream-status').textContent).toBe('connecting')
  })
})
