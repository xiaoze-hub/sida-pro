// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// A3 价格 Flash (既有组件 2026-09-05, 2026-09-10 A3 复用扩展时补行为测试):
// 值变大→up(红) / 变小→down(绿) / 首挂载/相同/null 不闪 / reduced-motion 不闪。
import FlashValue from '@panwatch/biz-ui/components/FlashValue'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function mockReducedMotion(reduced: boolean) {
  vi.spyOn(window, 'matchMedia').mockImplementation((q: string) => ({
    matches: reduced && q.includes('prefers-reduced-motion'),
    media: q,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }) as MediaQueryList)
}

describe('FlashValue', () => {
  it('值变大 → sida-flash-up; 变小 → sida-flash-down', () => {
    mockReducedMotion(false)
    const { container, rerender } = render(<FlashValue value={100}>100</FlashValue>)
    expect(container.querySelector('.sida-flash')).toBeNull() // 首挂载不闪

    rerender(<FlashValue value={101}>101</FlashValue>)
    expect(container.querySelector('.sida-flash-up')).toBeTruthy()

    rerender(<FlashValue value={100}>100</FlashValue>)
    expect(container.querySelector('.sida-flash-down')).toBeTruthy()
  })

  it('值相同 / null-both → 不闪', () => {
    mockReducedMotion(false)
    const { container, rerender } = render(<FlashValue value={100}>100</FlashValue>)
    rerender(<FlashValue value={100}>100</FlashValue>)
    expect(container.querySelector('.sida-flash')).toBeNull()

    rerender(<FlashValue value={null}>--</FlashValue>)
    rerender(<FlashValue value={null}>--</FlashValue>)
    expect(container.querySelector('.sida-flash')).toBeNull()
  })

  it('null → 有值 的首个有效值不闪(prev 为 null 视为基准)', () => {
    mockReducedMotion(false)
    const { container, rerender } = render(<FlashValue value={null}>--</FlashValue>)
    rerender(<FlashValue value={100}>100</FlashValue>)
    expect(container.querySelector('.sida-flash')).toBeNull()
  })

  it('prefers-reduced-motion → 永不闪(无障碍)', () => {
    mockReducedMotion(true)
    const { container, rerender } = render(<FlashValue value={100}>100</FlashValue>)
    rerender(<FlashValue value={101}>101</FlashValue>)
    expect(container.querySelector('.sida-flash')).toBeNull()
  })
})
