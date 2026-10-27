// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { healthApi } from '@sida/api'

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear() })
it('按事件增量派发，支持跨字节分块，收到完成事件立即结束而不等待连接关闭', async () => {
  const updates: unknown[] = []
  let cancelled = false
  const events = [{ nodes: [{ id: 'api', label: '应用服务' }], done_count: 1, total: 9, checked_at: 'now' }, { nodes: [], done_count: 9, total: 9, checked_at: 'now', done: true }]
  const bytes = new TextEncoder().encode(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(''))
  const body = new ReadableStream({ start(controller) { for (let index = 0; index < bytes.length; index += 7) controller.enqueue(bytes.slice(index, index + 7)) }, cancel() { cancelled = true } })
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, body })))
  await healthApi.streamSystemStatus(update => updates.push(update), new AbortController().signal)
  expect(updates).toEqual(events)
  expect(cancelled).toBe(true)
})
it('无完成事件的中断不能报告刷新成功', async () => {
  const body = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('data: {"nodes":[],"done_count":0,"total":9}\n\n')); controller.close() } })
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, body })))
  await expect(healthApi.streamSystemStatus(() => {}, new AbortController().signal)).rejects.toThrow('状态更新中断')
})
