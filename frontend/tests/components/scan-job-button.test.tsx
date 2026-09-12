// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  user: { demo: false, guest: false },
  toast: vi.fn(),
}))
vi.mock('@panwatch/api', () => ({ fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a) }))
vi.mock('@/lib/jwt', () => ({ isDemoUser: () => mocks.user.demo, isGuestUser: () => mocks.user.guest }))
vi.mock('@panwatch/base-ui/components/ui/toast', () => ({ useToast: () => ({ toast: mocks.toast }) }))

import ScanJobButton from '@/components/ScanJobButton'

describe('ScanJobButton (KI-053: 作业面板要有一个真的能点的入口)', () => {
  beforeEach(() => {
    mocks.fetchAPI.mockReset()
    mocks.toast.mockReset()
    mocks.user.demo = false
    mocks.user.guest = false
  })
  afterEach(() => cleanup())

  it('只读档(演示/访客)不显示触发入口', () => {
    mocks.user.guest = true
    const { container } = render(<ScanJobButton path="/theme-mood/scan/run" />)
    expect(container.firstChild).toBeNull()
  })

  it('点击起任务并提示去任务面板看进度', async () => {
    mocks.fetchAPI.mockResolvedValue({ started: true, job_id: 'j1' })
    render(<ScanJobButton path="/theme-mood/scan/run" />)
    fireEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(mocks.fetchAPI).toHaveBeenCalledWith('/theme-mood/scan/run', { method: 'POST' }))
    expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('系统 → 任务'), 'success')
  })

  it('单飞命中(已有任务在跑)时转达后端原因, 不谎报已开始', async () => {
    mocks.fetchAPI.mockResolvedValue({ started: false, reason: '扫描进行中', job_id: 'j1' })
    render(<ScanJobButton path="/theme-mood/scan/run" />)
    fireEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(mocks.toast).toHaveBeenCalled())
    expect(mocks.toast).toHaveBeenCalledWith('扫描进行中', 'info')
    expect(mocks.toast).not.toHaveBeenCalledWith(expect.stringContaining('已开始'), 'success')
  })

  it('任务跑完后回调 onDone 让宿主页刷新读侧', async () => {
    vi.useFakeTimers()
    try {
      mocks.fetchAPI.mockImplementation((p: string) =>
        Promise.resolve(p.startsWith('/jobs/') ? { status: 'succeeded' } : { started: true, job_id: 'j1' }))
      const onDone = vi.fn()
      render(<ScanJobButton path="/theme-mood/scan/run" onDone={onDone} />)
      fireEvent.click(screen.getByRole('button'))
      await vi.advanceTimersByTimeAsync(3100)
      expect(onDone).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
