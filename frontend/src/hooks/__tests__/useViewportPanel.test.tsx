// @vitest-environment jsdom
/**
 * useViewportPanel 契约测试 — 2026-10-27 修「手机输入法弹起遮住输入框」
 *
 * 锁死三件事（都是这次修掉的真实成因）：
 *  ① 键盘弹起让可视区域从 800 缩到 400 时，面板高度必须跟着缩（不然底部输入框在键盘下面）；
 *  ② 漏算 `visualViewport.offsetTop` 会把高度算大 → 不可接受，offsetTop 必须计入；
 *  ③ 某些浏览器键盘弹起只发 `scroll`、不发 `resize` → 只听 resize 会停在旧高度。
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { useViewportPanel } from '../useViewportPanel'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function Panel({ enabled = true }: { enabled?: boolean }) {
  const ref = useViewportPanel(enabled)
  return <div ref={ref} data-testid="panel" />
}

/** 假 visualViewport: 能改 height/offsetTop, 也能当事件目标用 */
function installVisualViewport(height: number, offsetTop = 0) {
  const target = new EventTarget() as EventTarget & { height: number; offsetTop: number }
  target.height = height
  target.offsetTop = offsetTop
  Object.defineProperty(window, 'visualViewport', { configurable: true, value: target })
  return target
}

const waitFrame = () => new Promise((r) => setTimeout(r, 30))

beforeEach(() => {
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 })
})

afterEach(() => {
  cleanup()
  Reflect.deleteProperty(window, 'visualViewport')
})

describe('useViewportPanel 手机键盘', () => {
  it('键盘弹起(可视高度 800→400)后高度跟着缩', async () => {
    const vv = installVisualViewport(800)
    const { getByTestId } = render(<Panel />)
    const panel = getByTestId('panel')
    // jsdom 没有布局: 手给一个面板矩形(顶部 100px, 有宽度)
    panel.getBoundingClientRect = () =>
      ({ top: 100, left: 0, width: 390, height: 0, right: 390, bottom: 0, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect

    vv.dispatchEvent(new Event('resize'))
    await waitFrame()
    expect(panel.style.height).toBe('684px') // 800 - 100 - 0(底部导航) - 16

    vv.height = 400 // 键盘弹起
    vv.dispatchEvent(new Event('resize'))
    await waitFrame()
    expect(panel.style.height).toBe('284px') // 400 - 100 - 16
  })

  it('offsetTop 必须计入: 可视窗口被上推 60px 时高度多留 60px', async () => {
    const vv = installVisualViewport(400, 0)
    const { getByTestId } = render(<Panel />)
    const panel = getByTestId('panel')
    panel.getBoundingClientRect = () =>
      ({ top: 100, left: 0, width: 390, height: 0, right: 390, bottom: 0, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect

    vv.dispatchEvent(new Event('resize'))
    await waitFrame()
    expect(panel.style.height).toBe('284px')

    vv.offsetTop = 60
    vv.dispatchEvent(new Event('resize'))
    await waitFrame()
    expect(panel.style.height).toBe('344px')
  })

  it('只发 scroll 不发 resize 的浏览器也要重算', async () => {
    const vv = installVisualViewport(800)
    const { getByTestId } = render(<Panel />)
    const panel = getByTestId('panel')
    panel.getBoundingClientRect = () =>
      ({ top: 100, left: 0, width: 390, height: 0, right: 390, bottom: 0, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect

    vv.height = 420
    vv.dispatchEvent(new Event('scroll')) // 只有 scroll
    await waitFrame()
    expect(panel.style.height).toBe('304px')
  })

  it('可见区域极小(横屏键盘)时不再硬撑 240px', async () => {
    const vv = installVisualViewport(320)
    const { getByTestId } = render(<Panel />)
    const panel = getByTestId('panel')
    panel.getBoundingClientRect = () =>
      ({ top: 100, left: 0, width: 390, height: 0, right: 390, bottom: 0, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect

    vv.dispatchEvent(new Event('resize'))
    await waitFrame()
    expect(panel.style.height).toBe('204px')
  })

  it('enabled=false 时清掉内联高度(嵌在别处的迷你工作台不受影响)', () => {
    const { getByTestId } = render(<Panel enabled={false} />)
    const panel = getByTestId('panel')
    panel.style.height = '500px'
    cleanup()
    render(<Panel enabled={false} />)
    expect(document.querySelector('[data-testid="panel"]')?.getAttribute('style') || '').not.toContain('height')
  })
})
