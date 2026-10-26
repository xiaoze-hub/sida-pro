// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
const mocks = vi.hoisted(() => ({ toggle: vi.fn() }))
vi.mock('@/pages/settings/context', () => ({ useSettings: () => ({ currentUser: { role: 'member' }, subscriptions: [{ report_type: 'review', label: '盘后复盘', enabled: true }], subLoading: false, sectionMatches: () => true, toggleSubscription: mocks.toggle }) }))
vi.mock('@/components/UserManagement', () => ({ default: () => null }))
import { UsersSection } from '@/pages/settings/UsersSection'
afterEach(() => { cleanup(); vi.clearAllMocks() })
describe('报告订阅开关', () => {
  it('使用统一 Switch 并具有明确名称，点击沿用原订阅更新接口', () => {
    render(<UsersSection activeId="sec-subscriptions" />)
    const control = screen.getByRole('switch', { name: '订阅 盘后复盘' })
    expect(control.getAttribute('aria-checked')).toBe('true')
    expect(control.classList.contains('inline-flex')).toBe(true)
    fireEvent.click(control)
    expect(mocks.toggle).toHaveBeenCalledWith('review')
  })
})
