// @vitest-environment jsdom
/**
 * 「申请 Pro」转化入口（2026-10-27）。
 *
 * 动因：普通用户撞到权限门时，页面上只有一行静态提示，没有出路。
 * 这组用例钉住三件事：
 *  ① 未登录 → 先去注册（转化 ≤2 步），不直接调申请接口；
 *  ② 已登录未申请 → 点一下就提交，并把状态切成「申请审核中」（不重复提交）；
 *  ③ 已开通 / 被拒 → 文案与可点性如实反映后端状态，取不到状态时**不猜**（保持可点）。
 */
import { MemoryRouter } from 'react-router-dom'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  applyPro: vi.fn(),
  fetchProApplyStatus: vi.fn(),
  isAuthenticated: vi.fn(() => true),
}))

vi.mock('@sida/api', () => ({
  applyPro: api.applyPro,
  fetchProApplyStatus: api.fetchProApplyStatus,
  isAuthenticated: api.isAuthenticated,
}))

import { ToastProvider } from '@sida/base-ui/components/ui/toast'
import { ProApplyCTA } from '@/components/ProApplyCTA'
import { NoAccessPage } from '@/components/NoAccessPage'

function status(over: Partial<{ role: string; app: string | null }> = {}) {
  const { role = 'member', app = null } = over
  return {
    role,
    has_application: app != null,
    application: app ? { id: 1, status: app, reason: '', contact: '', created_at: null, reviewed_at: null, review_note: '' } : null,
  }
}

function renderCTA(props: Parameters<typeof ProApplyCTA>[0] = {}) {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ProApplyCTA {...props} />
      </ToastProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  api.applyPro.mockReset().mockResolvedValue({ id: 7, status: 'pending' })
  api.fetchProApplyStatus.mockReset().mockResolvedValue(status())
  api.isAuthenticated.mockReset().mockReturnValue(true)
})

afterEach(cleanup)

describe('ProApplyCTA', () => {
  it('未登录：引导注册，不调申请接口', () => {
    api.isAuthenticated.mockReturnValue(false)
    renderCTA()
    const link = screen.getByRole('link', { name: /注册后申请 Pro/ })
    expect(link.getAttribute('href')).toBe('/login?mode=register')
    expect(api.fetchProApplyStatus).not.toHaveBeenCalled()
    expect(api.applyPro).not.toHaveBeenCalled()
  })

  it('已登录未申请：点击即提交，并把状态切成审核中', async () => {
    api.fetchProApplyStatus
      .mockResolvedValueOnce(status())
      .mockResolvedValueOnce(status({ app: 'pending' }))
    renderCTA({ reason: '权限不足：市场扫描' })
    const button = await screen.findByRole('button', { name: /申请开通 Pro/ })
    fireEvent.click(button)
    await waitFor(() => expect(api.applyPro).toHaveBeenCalledWith('权限不足：市场扫描'))
    expect((await screen.findByRole('button', { name: /申请审核中/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('审核中：进来就是禁用态，且不会重复提交', async () => {
    api.fetchProApplyStatus.mockResolvedValue(status({ app: 'pending' }))
    renderCTA()
    const button = await screen.findByRole('button', { name: /申请审核中/ }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(api.applyPro).not.toHaveBeenCalled()
  })

  it('已开通：显示已开通，不再给申请按钮', async () => {
    api.fetchProApplyStatus.mockResolvedValue(status({ role: 'pro', app: 'approved' }))
    renderCTA()
    expect(await screen.findByText(/已开通 Pro/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /申请/ })).toBeNull()
  })

  it('owner 角色：后端会 400 拒绝申请，前端必须直接显示已开通', async () => {
    api.fetchProApplyStatus.mockResolvedValue(status({ role: 'owner' }))
    renderCTA()
    expect(await screen.findByText(/已开通 Pro/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /申请/ })).toBeNull()
    expect(api.applyPro).not.toHaveBeenCalled()
  })

  it('已开通 + hideWhenApproved：整枚芯片不渲染（避免"需要更高权限…已开通 Pro"自相矛盾）', async () => {
    api.fetchProApplyStatus.mockResolvedValue(status({ role: 'owner' }))
    const { container } = renderCTA({ hideWhenApproved: true })
    await waitFor(() => expect(api.fetchProApplyStatus).toHaveBeenCalled())
    expect(container.querySelector('[data-pro-apply-cta]')).toBeNull()
  })

  it('被拒：显示「重新申请」且可点', async () => {
    api.fetchProApplyStatus.mockResolvedValue(status({ app: 'rejected' }))
    renderCTA()
    const button = await screen.findByRole('button', { name: /重新申请/ }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
  })

  it('状态接口取不到时不猜：保持可点，点了会拿到真实结果', async () => {
    api.fetchProApplyStatus.mockRejectedValue(new Error('offline'))
    renderCTA()
    const button = await screen.findByRole('button', { name: /申请开通 Pro/ }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
  })
})

describe('NoAccessPage', () => {
  it('说明缺什么权限，并给出申请入口（原来只是静默跳回首页）', async () => {
    render(
      <MemoryRouter>
        <ToastProvider>
          <NoAccessPage perm="view_opportunities" />
        </ToastProvider>
      </MemoryRouter>,
    )
    expect(screen.getAllByText(/市场扫描与集合竞价/).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('view_opportunities')).toBeTruthy()
    expect(await screen.findByRole('button', { name: /申请开通「市场扫描与集合竞价」权限/ })).toBeTruthy()
    expect(screen.getByRole('link', { name: /先回首页/ }).getAttribute('href')).toBe('/')
  })

  it('未知权限码照原样显示，不编中文名', () => {
    render(
      <MemoryRouter>
        <ToastProvider>
          <NoAccessPage perm="some_new_perm" />
        </ToastProvider>
      </MemoryRouter>,
    )
    expect(screen.getByText('some_new_perm')).toBeTruthy()
    expect(screen.getByRole('button', { name: /申请开通「some_new_perm」权限/ })).toBeTruthy()
  })
})

describe('ErrorState 权限类错误', () => {
  it('permission 类型会挂申请入口，network 类型不会', async () => {
    const { ErrorState } = await import('@/components/ErrorState')
    const { unmount } = render(
      <MemoryRouter>
        <ToastProvider>
          <ErrorState type="permission" />
        </ToastProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByRole('button', { name: /申请开通权限/ })).toBeTruthy()
    unmount()

    render(
      <MemoryRouter>
        <ToastProvider>
          <ErrorState type="network" />
        </ToastProvider>
      </MemoryRouter>,
    )
    expect(screen.queryByRole('button', { name: /申请开通权限/ })).toBeNull()
  })
})
