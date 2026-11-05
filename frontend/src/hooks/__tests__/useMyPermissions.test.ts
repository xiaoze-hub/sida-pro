// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ getMyPermissions: vi.fn() }))
vi.mock('@sida/api', () => ({ getMyPermissions: mocks.getMyPermissions }))
import { useMyPermissions } from '../useMyPermissions'

beforeEach(() => mocks.getMyPermissions.mockReset())

describe('登录态变化的权限加载', () => {
  it('登录后读取权限，换号时立即清除旧账号权限', async () => {
    mocks.getMyPermissions.mockResolvedValueOnce({ effective: ['view_quotes'] })
    let resolveSecond!: (value: unknown) => void
    mocks.getMyPermissions.mockImplementationOnce(() => new Promise((resolve) => { resolveSecond = resolve }))
    const hook = renderHook(({ token }) => useMyPermissions(token), { initialProps: { token: null as string | null } })
    expect(mocks.getMyPermissions).not.toHaveBeenCalled()
    hook.rerender({ token: 'account-a' })
    await waitFor(() => expect(hook.result.current.permissions?.has('view_quotes')).toBe(true))
    hook.rerender({ token: 'account-b' })
    expect(hook.result.current.permissions).toBeNull()
    await act(async () => resolveSecond({ effective: [] }))
    expect(hook.result.current.permissions?.size).toBe(0)
  })

  it('读取失败保持拒绝，用户可重新读取', async () => {
    mocks.getMyPermissions.mockResolvedValueOnce(null).mockResolvedValueOnce({ effective: ['view_quotes'] })
    const hook = renderHook(() => useMyPermissions('account-a'))
    await waitFor(() => expect(hook.result.current.failed).toBe(true))
    expect(hook.result.current.permissions).toBeNull()
    act(() => hook.result.current.retry())
    await waitFor(() => expect(hook.result.current.permissions?.has('view_quotes')).toBe(true))
    expect(hook.result.current.failed).toBe(false)
  })

  it('旧账号权限请求迟到时，不覆盖当前账号', async () => {
    let resolveOld!: (value: unknown) => void
    mocks.getMyPermissions
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
      .mockResolvedValueOnce({ effective: [] })
    const hook = renderHook(({ token }) => useMyPermissions(token), { initialProps: { token: 'account-a' } })
    hook.rerender({ token: 'account-b' })
    await waitFor(() => expect(hook.result.current.permissions?.size).toBe(0))
    await act(async () => resolveOld({ effective: ['view_quotes'] }))
    expect(hook.result.current.permissions?.has('view_quotes')).toBe(false)
  })
})
