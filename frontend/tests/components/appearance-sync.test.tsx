// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setAppearance, useTheme } from '@/hooks/use-theme'

beforeEach(() => {
  localStorage.clear()
  document.documentElement.className = ''
  document.documentElement.removeAttribute('data-theme')
  vi.stubGlobal('matchMedia', vi.fn(() => ({
    matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('全站外观状态同步', () => {
  it('设置页的选择同步到其他入口，并保持存储', () => {
    const a = renderHook(useTheme)
    const b = renderHook(useTheme)
    act(() => a.result.current.setMode('light'))
    expect(b.result.current.mode).toBe('light')
    expect(document.documentElement.classList.contains('light')).toBe(true)
    expect(localStorage.getItem('sida-theme')).toBe('light')
    act(() => setAppearance({ density: 'comfortable' }))
    expect(a.result.current.density).toBe('comfortable')
    expect(b.result.current.density).toBe('comfortable')
    expect(document.documentElement.dataset.density).toBe('comfortable')
  })
  it('舒适模式不会污染浅色；切回深色时恢复偏好', () => {
    const hook = renderHook(useTheme)
    act(() => setAppearance({ mode: 'light', dim: true }))
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
    act(() => hook.result.current.setMode('dark'))
    expect(document.documentElement.dataset.theme).toBe('dim')
    act(() => hook.result.current.toggleTheme())
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })
  it('其他窗口修改主题后，同步更新当前界面', () => {
    const hook = renderHook(useTheme)
    act(() => {
      localStorage.setItem('sida-theme', 'light')
      window.dispatchEvent(new StorageEvent('storage', { key: 'sida-theme' }))
    })
    expect(hook.result.current.mode).toBe('light')
  })
  it('重新挂载能恢复持久化偏好', () => {
    act(() => setAppearance({ mode: 'dark', density: 'compact', dim: true }))
    const hook = renderHook(useTheme)
    expect(hook.result.current.mode).toBe('dark')
    expect(hook.result.current.density).toBe('compact')
    expect(document.documentElement.dataset.theme).toBe('dim')
  })
})
