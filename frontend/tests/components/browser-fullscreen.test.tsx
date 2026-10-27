// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import BrowserFullscreenButton from '@/components/BrowserFullscreenButton'
import { I18nProvider } from '@/hooks/useI18n'

const { toast } = vi.hoisted(() => ({ toast: vi.fn() }))
vi.mock('@panwatch/base-ui/components/ui/toast', () => ({ useToast: () => ({ toast }) }))
let fullscreen: Element | null = null
const originals = [
  [document, 'fullscreenEnabled'], [document, 'fullscreenElement'], [document, 'exitFullscreen'],
  [document.documentElement, 'requestFullscreen'],
] as const
const descriptors = originals.map(([object, key]) => Object.getOwnPropertyDescriptor(object, key))
const enter = vi.fn(async () => { fullscreen = document.documentElement; document.dispatchEvent(new Event('fullscreenchange')) })
const exit = vi.fn(async () => { fullscreen = null; document.dispatchEvent(new Event('fullscreenchange')) })

beforeEach(() => {
  fullscreen = null
  enter.mockClear(); exit.mockClear(); toast.mockClear()
  Object.defineProperties(document, {
    fullscreenEnabled: { configurable: true, value: true },
    fullscreenElement: { configurable: true, get: () => fullscreen },
    exitFullscreen: { configurable: true, value: exit },
  })
  Object.defineProperty(document.documentElement, 'requestFullscreen', { configurable: true, value: enter })
})
afterEach(() => {
  cleanup()
  originals.forEach(([object, key], index) => {
    const descriptor = descriptors[index]
    if (descriptor) Object.defineProperty(object, key, descriptor)
    else Reflect.deleteProperty(object, key)
  })
})

it('对整个文档进入/退出原生全屏，并同步两个入口', async () => {
  render(<><BrowserFullscreenButton /><BrowserFullscreenButton /></>)
  fireEvent.click(screen.getAllByRole('button', { name: '浏览器全屏' })[0])
  await waitFor(() => expect(screen.getAllByRole('button', { name: '退出浏览器全屏' })).toHaveLength(2))
  expect(enter).toHaveBeenCalledTimes(1)
  expect(fullscreen).toBe(document.documentElement)
  fireEvent.click(screen.getAllByRole('button', { name: '退出浏览器全屏' })[1])
  await waitFor(() => expect(screen.getAllByRole('button', { name: '浏览器全屏' })).toHaveLength(2))
  expect(exit).toHaveBeenCalledTimes(1)
})

it('Esc 或浏览器菜单退出时跟随 fullscreenchange 恢复图标状态', () => {
  fullscreen = document.documentElement
  render(<BrowserFullscreenButton />)
  expect(screen.getByRole('button', { name: '退出浏览器全屏' }).getAttribute('aria-pressed')).toBe('true')
  act(() => { fullscreen = null; document.dispatchEvent(new Event('fullscreenchange')) })
  expect(screen.getByRole('button', { name: '浏览器全屏' }).getAttribute('aria-pressed')).toBe('false')
})

it('浏览器拒绝请求后保留真实状态并允许重试', async () => {
  enter.mockRejectedValueOnce(new Error('NotAllowedError'))
  render(<BrowserFullscreenButton />)
  fireEvent.click(screen.getByRole('button', { name: '浏览器全屏' }))
  await waitFor(() => expect(toast).toHaveBeenCalledWith('浏览器未能切换全屏，请重试或使用浏览器菜单。', 'error'))
  const button = screen.getByRole('button', { name: '浏览器全屏' }) as HTMLButtonElement
  expect(button.disabled).toBe(false)
  fireEvent.click(button)
  await screen.findByRole('button', { name: '退出浏览器全屏' })
})

it('原生全屏请求未完成时防止连续提交', async () => {
  let finish!: () => void
  enter.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve }))
  render(<BrowserFullscreenButton />)
  const button = screen.getByRole('button', { name: '浏览器全屏' })
  fireEvent.click(button); fireEvent.click(button)
  expect(enter).toHaveBeenCalledTimes(1)
  await act(async () => finish())
})

it('不支持原生全屏的浏览器隐藏入口，支持英文名称', () => {
  Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: false })
  const view = render(<BrowserFullscreenButton />)
  expect(screen.queryByRole('button')).toBeNull()
  Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: true })
  view.rerender(<I18nProvider initialLocale="en-US"><BrowserFullscreenButton /></I18nProvider>)
  expect(screen.getByRole('button', { name: 'Browser fullscreen' })).toBeTruthy()
})
