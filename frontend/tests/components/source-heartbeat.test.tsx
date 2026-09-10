// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

// C1 顶部源心跳条 (2026-09-10): 色段/汇总/EWMA 读数 + 空态/失败态三态。
const mocks = vi.hoisted(() => ({ useVendorTrust: vi.fn() }))
vi.mock('@/hooks/useVendorTrust', () => ({ useVendorTrust: mocks.useVendorTrust }))

import SourceHeartbeat from '../../src/components/SourceHeartbeat'

afterEach(() => {
  cleanup()
  mocks.useVendorTrust.mockReset()
})

function renderBar() {
  return render(
    <MemoryRouter>
      <SourceHeartbeat />
    </MemoryRouter>,
  )
}

describe('SourceHeartbeat', () => {
  it('渲染每源色段 + 汇总 + 首源 EWMA 读数, 悬停 tooltip 带明细', () => {
    mocks.useVendorTrust.mockReturnValue({
      items: [
        { vendor: 'tq', score: 98, success_rate: 0.98, p50_latency_ms: 30, ewma_latency_ms: 28, samples: 100 },
        { vendor: 'tencent', score: 60, success_rate: 0.7, p50_latency_ms: 900, ewma_latency_ms: 1200, samples: 50 },
        { vendor: 'never', score: null, success_rate: null, ewma_latency_ms: null, samples: 0 },
      ],
      loading: false,
      error: false,
    })
    renderBar()
    const bar = screen.getByTestId('source-heartbeat')
    expect(bar.querySelectorAll('[data-vendor]').length).toBe(3)
    expect(bar.querySelector('[data-vendor="tq"]')?.getAttribute('data-tone')).toBe('ok')
    expect(bar.querySelector('[data-vendor="tencent"]')?.getAttribute('data-tone')).toBe('warn')
    expect(bar.querySelector('[data-vendor="never"]')?.getAttribute('data-tone')).toBe('unknown')
    expect(bar.textContent).toContain('3 源')
    // 98→ok, 60→warn, null→unknown ⇒ 1 正常 · 1 需关注 · 1 无样本
    expect(bar.textContent).toContain('1 正常')
    expect(bar.textContent).toContain('1 需关注')
    expect(bar.textContent).toContain('1 无样本')
    expect(bar.textContent).toContain('28ms') // 首源 EWMA 直读
    expect(bar.querySelector('[data-vendor="tq"]')?.getAttribute('title')).toContain('EWMA')
  })

  it('从未有样本 → 不渲染(无源可报≠故障, 避免空条噪音)', () => {
    mocks.useVendorTrust.mockReturnValue({ items: [], loading: false, error: false })
    const { container } = renderBar()
    expect(container.textContent).toBe('')
  })

  it('请求失败且无快照 → 显式提示(不静默)', () => {
    mocks.useVendorTrust.mockReturnValue({ items: [], loading: false, error: true })
    renderBar()
    expect(screen.getByText(/源心跳不可用/)).toBeTruthy()
  })

  it('刷新失败但有快照 → 保留读数并显式标注', () => {
    mocks.useVendorTrust.mockReturnValue({
      items: [{ vendor: 'tq', score: 98, ewma_latency_ms: 28, samples: 10 }],
      loading: false,
      error: true,
    })
    renderBar()
    expect(screen.getByTestId('source-heartbeat').textContent).toContain('28ms')
    expect(screen.getByText(/刷新失败/)).toBeTruthy()
  })
})
