// @vitest-environment jsdom
// KI-025: 行情 WS 消费 hook —— envelope 解析 / SWP 鉴权 / last_seq 补发 / 指数退避 / 4401 不重连。
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@panwatch/api', () => ({ getToken: () => 'test.jwt.token' }))

import { useQuoteStream } from '@/realtime/useQuoteStream'

class FakeWS {
  static instances: FakeWS[] = []
  url: string
  protocols?: string | string[]
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: ((ev: { code: number }) => void) | null = null
  onerror: (() => void) | null = null

  constructor(url: string, protocols?: string | string[]) {
    this.url = url
    this.protocols = protocols
    FakeWS.instances.push(this)
  }
  close() {}
  open() {
    this.onopen?.()
  }
  msg(obj: unknown) {
    this.onmessage?.({ data: JSON.stringify(obj) })
  }
  fireClose(code = 1006) {
    this.onclose?.({ code })
  }
}

const TICK = { '600519': { price: 1288.5, change_pct: -1.2, prev_close: 1304.0, name: '贵州茅台' } }

describe('useQuoteStream (KI-025)', () => {
  beforeEach(() => {
    FakeWS.instances = []
    vi.stubGlobal('WebSocket', FakeWS as unknown as typeof WebSocket)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('用 SWP 子协议携带 token, URL 里不带 token', () => {
    const onTicks = vi.fn()
    renderHook(() => useQuoteStream(onTicks))
    const ws = FakeWS.instances[0]
    expect(ws).toBeTruthy()
    expect(ws.protocols).toEqual(['panwatch.auth.bearer', 'test.jwt.token'])
    expect(ws.url).toContain('/api/quotes/ws')
    expect(ws.url).not.toContain('token=')
  })

  it('解析 envelope quote.tick 帧并回调 data', () => {
    const onTicks = vi.fn()
    const { result } = renderHook(() => useQuoteStream(onTicks))
    const ws = FakeWS.instances[0]
    act(() => ws.open())
    expect(result.current).toBe('live')
    act(() => ws.msg({ seq: 7, ts: 1, topic: 'quote.tick', user_id: 'u1', payload: { type: 'quotes', data: TICK } }))
    expect(onTicks).toHaveBeenCalledWith(TICK)
  })

  it('兼容旧裸帧(无 seq)与 quote.snapshot', () => {
    const onTicks = vi.fn()
    renderHook(() => useQuoteStream(onTicks))
    const ws = FakeWS.instances[0]
    act(() => ws.open())
    act(() => ws.msg({ type: 'quotes', data: TICK }))
    act(() => ws.msg({ seq: 1, ts: 1, topic: 'quote.snapshot', user_id: 'u1', payload: { type: 'snapshot', data: TICK } }))
    expect(onTicks).toHaveBeenCalledTimes(2)
  })

  it('非行情帧不回调', () => {
    const onTicks = vi.fn()
    renderHook(() => useQuoteStream(onTicks))
    const ws = FakeWS.instances[0]
    act(() => ws.open())
    act(() => ws.msg({ seq: 2, ts: 1, topic: 'notif.push', user_id: 'u1', payload: { type: 'other', data: TICK } }))
    expect(onTicks).not.toHaveBeenCalled()
  })

  it('断线重连带 last_seq, 退避递增(1s → 2s)', () => {
    vi.useFakeTimers()
    const onTicks = vi.fn()
    renderHook(() => useQuoteStream(onTicks))
    const ws1 = FakeWS.instances[0]
    act(() => ws1.open())
    act(() => ws1.msg({ seq: 42, ts: 1, topic: 'quote.tick', user_id: 'u1', payload: { type: 'quotes', data: TICK } }))
    act(() => ws1.fireClose(1006))

    act(() => vi.advanceTimersByTime(1000))
    expect(FakeWS.instances).toHaveLength(2)
    expect(FakeWS.instances[1].url).toContain('last_seq=42')

    // 第二次断线: 退避 2s, 1s 时不应重连
    act(() => FakeWS.instances[1].fireClose(1006))
    act(() => vi.advanceTimersByTime(1000))
    expect(FakeWS.instances).toHaveLength(2)
    act(() => vi.advanceTimersByTime(1000))
    expect(FakeWS.instances).toHaveLength(3)
  })

  it('4401 鉴权失败不重连', () => {
    vi.useFakeTimers()
    const onTicks = vi.fn()
    const { result } = renderHook(() => useQuoteStream(onTicks))
    const ws = FakeWS.instances[0]
    act(() => ws.open())
    act(() => ws.fireClose(4401))
    expect(result.current).toBe('offline')
    act(() => vi.advanceTimersByTime(60_000))
    expect(FakeWS.instances).toHaveLength(1)
  })

  it('enabled=false 时不建连', () => {
    const onTicks = vi.fn()
    const { result } = renderHook(() => useQuoteStream(onTicks, false))
    expect(FakeWS.instances).toHaveLength(0)
    expect(result.current).toBe('offline')
  })
})
