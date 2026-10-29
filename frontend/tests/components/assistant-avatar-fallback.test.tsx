// @vitest-environment jsdom
/**
 * 共享助手形象的兜底与状态映射（2026-10-29）。
 *
 * 历史问题：Lottie 资源 404/坏 JSON 时 `loadAnimation` 之前的 catch 只有一句空注释，
 * 结果是**入口位置彻底空白** —— 用户看不到形象，也看不出这页还活着。
 *
 * 现在钉住：
 *  1. 资源没到之前先是**静态球**（不闪空白），到了由动效层淡入接管；
 *  2. 资源失败 → 静态球留在原地并保留可访问名（aria-label），不重试风暴；
 *  3. `data-avatar-motion` 只反映**真实**状态（可见且未减少动效 = playing，
 *     隐藏页签/离屏/减少动效 = paused），不因为"看起来热闹"伪造 playing。
 */
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ play: vi.fn(), resume: vi.fn(), pause: vi.fn(), speed: vi.fn(), stop: vi.fn(), destroy: vi.fn(), load: vi.fn() }))
vi.mock('@/lib/assistant-avatar', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/assistant-avatar')>(),
  loadAssistantAnimation: mocks.load,
}))
import { AssistantAvatar } from '@/components/AssistantAvatar'

const animationStub = () => ({
  isLoaded: true,
  goToAndPlay: mocks.play,
  goToAndStop: mocks.stop,
  play: mocks.resume,
  pause: mocks.pause,
  setSpeed: mocks.speed,
  destroy: mocks.destroy,
  addEventListener: vi.fn(),
})

const instant = async () => ({ idleFrame: 60, data: {}, lottie: { loadAnimation: () => animationStub() } })

beforeEach(() => {
  localStorage.clear()
  mocks.play.mockReset(); mocks.resume.mockReset(); mocks.pause.mockReset()
  mocks.speed.mockReset(); mocks.stop.mockReset(); mocks.destroy.mockReset(); mocks.load.mockReset()
  mocks.load.mockImplementation(instant)
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('助手形象兜底', () => {
  it('资源到位前先给静态球，不是空白', async () => {
    mocks.load.mockImplementation(() => new Promise(() => {}))
    const view = render(<AssistantAvatar label="数智参谋" />)
    const root = view.container.querySelector('[data-assistant-avatar]') as HTMLElement
    expect(root.getAttribute('data-avatar-source')).toBe('loading')
    const still = view.container.querySelector('[data-avatar-static]') as HTMLElement
    expect(still).toBeTruthy()
    expect(still.style.opacity).not.toBe('0')
    // 动效层此时不可见（不闪现、也不占视觉）
    expect((view.container.querySelector('.assistant-avatar-motion') as HTMLElement).style.opacity).toBe('0')
  })

  it('资源失败时静态球留在原地并保留可访问名，不重复拉取', async () => {
    mocks.load.mockImplementation(() => Promise.reject(new Error('Animation asset unavailable')))
    const view = render(<AssistantAvatar label="数智参谋" />)
    const root = view.container.querySelector('[data-assistant-avatar]') as HTMLElement
    await waitFor(() => expect(root.getAttribute('data-avatar-source')).toBe('static'))
    expect(root.getAttribute('data-avatar-motion')).toBe('static')
    const still = view.container.querySelector('[data-avatar-static]') as HTMLElement
    expect(still).toBeTruthy()
    expect(still.style.opacity).not.toBe('0')
    expect(root.getAttribute('role')).toBe('img')
    expect(root.getAttribute('aria-label')).toBe('数智参谋')
    // 失败不重试（资源缺失时重试只会打满网络）
    expect(mocks.load).toHaveBeenCalledTimes(1)
  })

  it('资源到位后动效层接管，静态球退出（不叠双影）', async () => {
    const view = render(<AssistantAvatar />)
    const root = view.container.querySelector('[data-assistant-avatar]') as HTMLElement
    await waitFor(() => expect(root.getAttribute('data-avatar-source')).toBe('lottie'))
    expect((view.container.querySelector('[data-avatar-static]') as HTMLElement).style.opacity).toBe('0')
    expect((view.container.querySelector('.assistant-avatar-motion') as HTMLElement).style.opacity).toBe('1')
  })

  it('动效状态只映射真实状态：可见=playing，离屏/隐藏/减少动效=paused', async () => {
    let notify: ((entries: Array<{ isIntersecting: boolean }>) => void) | null = null
    class FakeObserver {
      constructor(cb: (entries: Array<{ isIntersecting: boolean }>) => void) { notify = cb }
      observe() {}
      disconnect() { notify = null }
    }
    vi.stubGlobal('IntersectionObserver', FakeObserver)
    const view = render(<AssistantAvatar />)
    const root = view.container.querySelector('[data-assistant-avatar]') as HTMLElement
    await waitFor(() => expect(root.getAttribute('data-avatar-source')).toBe('lottie'))
    expect(root.getAttribute('data-avatar-motion')).toBe('playing')

    act(() => notify?.([{ isIntersecting: false }]))
    expect(root.getAttribute('data-avatar-motion')).toBe('paused')

    act(() => notify?.([{ isIntersecting: true }]))
    expect(root.getAttribute('data-avatar-motion')).toBe('playing')

    // 页签切到后台：必须停
    const hidden = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    expect(root.getAttribute('data-avatar-motion')).toBe('paused')
    hidden.mockRestore()
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    view.unmount()
  })

  it('减少动效偏好下不声称在播放', async () => {
    vi.mocked(window.matchMedia).mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList)
    const view = render(<AssistantAvatar busy />)
    const root = view.container.querySelector('[data-assistant-avatar]') as HTMLElement
    await waitFor(() => expect(mocks.stop).toHaveBeenCalled())
    expect(root.getAttribute('data-avatar-motion')).toBe('paused')
    expect(root.getAttribute('data-animated')).toBe('false')
  })
})
