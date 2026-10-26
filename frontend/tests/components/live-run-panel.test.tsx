// @vitest-environment jsdom
/**
 * 运行实时面板：SSE 事件合流、终态关闭、断流不假装在跑。
 *
 * 口径红线：连不上事件流时必须显式说"已断开"，不能用"执行中"掩盖；
 * 耗时/工具数缺失一律 `--`，不补 0。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

type Handler = (ev: MessageEvent) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  listeners: Record<string, Handler[]> = {}
  closed = false
  onerror: (() => void) | null = null

  constructor(public url: string, public init?: EventSourceInit) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, fn: Handler) {
    ;(this.listeners[name] ||= []).push(fn)
  }

  emit(name: string, payload: unknown) {
    const ev = { data: JSON.stringify(payload) } as MessageEvent
    for (const fn of this.listeners[name] ?? []) fn(ev)
  }

  close() {
    this.closed = true
  }
}

import { LiveRunPanel, isTerminalStatus, reduceEvent } from '@/components/copilot/LiveRunPanel'

beforeEach(() => {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function last(): FakeEventSource {
  const es = FakeEventSource.instances[FakeEventSource.instances.length - 1]
  if (!es) throw new Error('没有建立 EventSource')
  return es
}

describe('LiveRunPanel', () => {
  it('按 run_id 订阅事件流（带 cookie 凭证，不把 token 拼进 URL）', () => {
    render(<LiveRunPanel runId="r-1" />)
    const es = last()
    expect(es.url).toBe('/api/copilot/runs/r-1/events')
    expect(es.init?.withCredentials).toBe(true)
  })

  it('状态与步骤逐步到达后就地更新', async () => {
    render(<LiveRunPanel runId="r-2" />)
    const es = last()
    es.emit('status', { status: 'running', tool_calls: 1, elapsed_ms: 1200 })
    es.emit('steps', { count: 2, items: [
      { seq: 1, tool: 'get_stock_quote', ok: true, elapsed_ms: 300 },
      { seq: 2, tool: 'get_technical_analysis', ok: true, elapsed_ms: 900 },
    ] })
    await waitFor(() => expect(screen.getByText('执行中')).toBeTruthy())
    expect(screen.getByText('get_stock_quote')).toBeTruthy()
    expect(screen.getByText('get_technical_analysis')).toBeTruthy()
  })

  it('终态到达后关闭事件流', async () => {
    render(<LiveRunPanel runId="r-3" />)
    const es = last()
    es.emit('status', { status: 'running' })
    es.emit('done', { status: 'ok', error: null })
    await waitFor(() => expect(es.closed).toBe(true))
    expect(screen.getByText('已完成')).toBeTruthy()
  })

  it('断流要显式说"已断开"，不能用执行中掩盖', async () => {
    render(<LiveRunPanel runId="r-4" />)
    const es = last()
    es.emit('status', { status: 'running' })
    es.onerror?.()
    await waitFor(() => expect(screen.getByText(/事件流已断开/)).toBeTruthy())
  })

  it('缺耗时显示 --，不补 0', () => {
    render(<LiveRunPanel runId="r-5" />)
    expect(screen.getAllByText('--').length).toBeGreaterThan(0)
  })

  it('没有 run_id 时不渲染也不建连', () => {
    const { container } = render(<LiveRunPanel runId={null} />)
    expect(container.firstChild).toBeNull()
    expect(FakeEventSource.instances.length).toBe(0)
  })

  it('reduceEvent 合并语义：步骤覆盖、状态以最新为准、error 保留', () => {
    const base = {
      status: 'queued', toolCalls: null, elapsedMs: null,
      steps: [], error: null, conversationId: null,
    }
    const a = reduceEvent(base, 'status', { status: 'running', tool_calls: 2 })
    expect(a.status).toBe('running')
    expect(a.toolCalls).toBe(2)
    const b = reduceEvent(a, 'steps', { items: [{ seq: 1, tool: 'x', ok: true, elapsed_ms: 5 }] })
    expect(b.steps.length).toBe(1)
    expect(b.status).toBe('running')
    const c = reduceEvent(b, 'error', { message: '事件流超时' })
    expect(c.error).toBe('事件流超时')
  })

  it('isTerminalStatus 认全五个终态', () => {
    for (const s of ['ok', 'succeeded', 'failed', 'degraded', 'cancelled']) {
      expect(isTerminalStatus(s)).toBe(true)
    }
    expect(isTerminalStatus('running')).toBe(false)
    expect(isTerminalStatus('')).toBe(false)
  })
})
