// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { healthApi, type SystemStatusUpdate } from '@panwatch/api'
import { useSystemStatus } from '@/hooks/useSystemStatus'
import { emptySystemStatus, readSystemStatusCache, saveSystemStatusCache } from '@/lib/system-status'

vi.mock('@panwatch/api', () => ({ healthApi: { streamSystemStatus: vi.fn(), selfcheckList: vi.fn(), selfcheckKeys: vi.fn() } }))
beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); sessionStorage.clear() })
afterEach(cleanup)
it('首次即渲染全部模块，等待时叠加更新进度，未检测不能显示正常', () => {
  vi.mocked(healthApi.streamSystemStatus).mockImplementation(() => new Promise(() => {}))
  const { result } = renderHook(() => useSystemStatus())
  expect(result.current.data.nodes).toHaveLength(9)
  expect(result.current.data.nodes.every(node => node.status === 'unknown')).toBe(true)
  expect(result.current.loading).toBe(true)
  expect(result.current.refreshProgress.done).toBe(0)
  expect(healthApi.selfcheckKeys).not.toHaveBeenCalled()
})
it('先显示账号缓存与时间，逐项覆盖，未更新节点保留原值但不点亮连接', async () => {
  localStorage.setItem('user', JSON.stringify({ id: 'owner' }))
  const cached = emptySystemStatus()
  cached.checked_at = '2026-10-26T12:00:00Z'
  cached.nodes = cached.nodes.map(node => ({ ...node, status: 'ok', connected: true }))
  saveSystemStatusCache(cached)
  let emit!: (update: SystemStatusUpdate) => void
  let reject!: (error: Error) => void
  vi.mocked(healthApi.streamSystemStatus).mockImplementation(callback => { emit = callback; return new Promise((_resolve, fail) => { reject = fail }) })
  const { result } = renderHook(() => useSystemStatus())
  expect(result.current.data.checked_at).toBe(cached.checked_at)
  expect(result.current.data.nodes.every(node => !node.connected)).toBe(true)
  const updated = { ...cached.nodes.find(node => node.id === 'database')!, status: 'fail' as const, connected: false }
  act(() => emit({ nodes: [updated], done_count: 1, total: 9, checked_at: '2026-10-26T13:00:00Z' }))
  expect(result.current.data.nodes.find(node => node.id === 'database')?.status).toBe('fail')
  expect(result.current.data.nodes.find(node => node.id === 'ai')?.status).toBe('ok')
  expect(result.current.pendingNodes).not.toContain('database')
  expect(result.current.pendingNodes).toContain('ai')
  expect(result.current.refreshProgress.done).toBe(1)
  await act(async () => reject(new Error('连接中断')))
  await waitFor(() => expect(result.current.error).toBe('连接中断'))
  expect(result.current.data.nodes.find(node => node.id === 'database')?.status).toBe('fail')
  expect(healthApi.selfcheckKeys).not.toHaveBeenCalled()
})
it('权限不足或页面未开启时不请求系统状态', () => {
  renderHook(() => useSystemStatus(false))
  expect(healthApi.streamSystemStatus).not.toHaveBeenCalled()
})
it('缓存按四个账号隔离，历史快照不会自动连通', () => {
  const snapshot = emptySystemStatus(); snapshot.checked_at = '2026-10-26T12:00:00Z'
  for (const id of ['one', 'two', 'three', 'four']) {
    localStorage.setItem('user', JSON.stringify({ id }))
    expect(readSystemStatusCache()).toBeNull()
    saveSystemStatusCache({ ...snapshot, version: id })
  }
  for (const id of ['one', 'two', 'three', 'four']) {
    localStorage.setItem('user', JSON.stringify({ id }))
    expect(readSystemStatusCache()?.version).toBe(id)
    expect(readSystemStatusCache()?.edges.some(edge => edge.connected)).toBe(false)
  }
})
