// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TabActiveProvider } from '@panwatch/base-ui'

const mocks = vi.hoisted(() => ({ play: vi.fn(), resume: vi.fn(), pause: vi.fn(), speed: vi.fn(), stop: vi.fn(), destroy: vi.fn(), load: vi.fn() }))
vi.mock('@/lib/assistant-avatar', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/assistant-avatar')>(),
  loadAssistantAnimation: mocks.load,
}))
import { AssistantAvatar } from '@/components/AssistantAvatar'
import { setAssistantAvatar } from '@/lib/assistant-avatar'

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  mocks.play.mockReset(); mocks.resume.mockReset(); mocks.pause.mockReset(); mocks.speed.mockReset(); mocks.stop.mockReset(); mocks.destroy.mockReset()
  mocks.load.mockImplementation(async () => ({
    idleFrame: 60, data: {}, lottie: { loadAnimation: () => ({ isLoaded: true, goToAndPlay: mocks.play, goToAndStop: mocks.stop, play: mocks.resume, pause: mocks.pause, setSpeed: mocks.speed, destroy: mocks.destroy, addEventListener: vi.fn() }) },
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('共享助手形象', () => {
  it('加载完成前不闪现旧静态图标', () => {
    mocks.load.mockImplementation(() => new Promise(() => {}))
    const view = render(<AssistantAvatar label="AI 助手" />)
    expect(view.container.querySelector('svg')).toBeNull()
    expect((view.container.querySelector('.assistant-avatar-motion') as HTMLElement).style.opacity).toBe('0')
  })

  it('默认第二款；空闲慢速循环，工作时提速，完成后慢速并销毁资源', async () => {
    const view = render(<AssistantAvatar />)
    await waitFor(() => expect(mocks.play).toHaveBeenCalledWith(60, true))
    expect(mocks.speed).toHaveBeenLastCalledWith(0.55)
    expect(view.container.querySelector('[data-assistant-avatar="foriday"]')).toBeTruthy()
    expect(view.container.querySelector('[data-animated="true"] .assistant-avatar-motion')).toBeTruthy()
    view.rerender(<AssistantAvatar busy />)
    expect(mocks.speed).toHaveBeenLastCalledWith(1)
    expect(mocks.resume).toHaveBeenCalled()
    view.rerender(<AssistantAvatar />)
    expect(view.container.querySelector('[data-animated="true"]')).toBeTruthy()
    expect(mocks.speed).toHaveBeenLastCalledWith(0.55)
    expect(mocks.play).toHaveBeenCalledTimes(1)
    view.unmount()
    expect(mocks.destroy).toHaveBeenCalledTimes(1)
  })
  it('减少动态效果和隐藏页签都不播放', async () => {
    vi.mocked(window.matchMedia).mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList)
    const view = render(<AssistantAvatar busy />)
    await waitFor(() => expect(mocks.stop).toHaveBeenCalled())
    expect(mocks.play).not.toHaveBeenCalled()
    view.unmount()
    vi.mocked(window.matchMedia).mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList)
    render(<TabActiveProvider active={false}><AssistantAvatar busy /></TabActiveProvider>)
    await waitFor(() => expect(mocks.stop.mock.calls.length).toBeGreaterThan(1))
    expect(mocks.play).not.toHaveBeenCalled()
  })
  it('同窗口所有入口同步切换，账号间独立保存', async () => {
    localStorage.setItem('user', JSON.stringify({ id: 'one' }))
    const view = render(<><AssistantAvatar /><AssistantAvatar /></>)
    act(() => setAssistantAvatar('flow'))
    expect(view.container.querySelectorAll('[data-assistant-avatar="flow"]').length).toBe(2)
    expect(localStorage.getItem('sida_assistant_avatar:one')).toBe('flow')
    act(() => { localStorage.setItem('user', JSON.stringify({ id: 'two' })); window.dispatchEvent(new Event('storage')) })
    expect(view.container.querySelectorAll('[data-assistant-avatar="foriday"]').length).toBe(2)
    expect(localStorage.getItem('sida_assistant_avatar:two')).toBeNull()
  })
})
