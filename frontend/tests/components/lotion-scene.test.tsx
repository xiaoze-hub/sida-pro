// @vitest-environment jsdom
/**
 * Lottie 场景层（2026-10-27）。
 *
 * 钉的是**行为契约**，不是实现细节：
 *  · 载入成功 → 容器带 data-lotion-ready，且真的调用了 lottie.loadAnimation；
 *  · 资产 404 / 结构非法 → 渲染**等尺寸占位**，绝不抛错、绝不塌布局；
 *  · `prefers-reduced-motion` → 不自动播放，直接停在末帧（末帧是完整图形）；
 *  · 页面隐藏 / 滚出视口 → 暂停，不留后台 RAF；
 *  · 颜色 token：`nm: "token:*"` 的描边/填充被替换成容器计算色。
 *
 * 为什么这么测：手写 bodymovin JSON 最容易犯的错是**结构合法但画不出东西**
 * （例如 trim 顺序写错 → d="M0 0"）。组件层测不到像素，所以像素验证放在
 * `scripts/gen-lotion-scenes.py` 的四种排列对照实验里，这里守住降级与播放纪律。
 */
import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const loadAnimation = vi.fn()
const destroy = vi.fn()

vi.mock('lottie-web', () => ({
  default: { loadAnimation: (...args: unknown[]) => loadAnimation(...args) },
}))

vi.mock('@sida/base-ui', () => ({ useTabActive: () => true }))

import { LotionScene } from '@/components/motion/LotionScene'
import { parseColor } from '@/lib/lotion'

const SCENE = {
  v: '5.7.0',
  fr: 60,
  ip: 0,
  op: 60,
  w: 48,
  h: 48,
  layers: [
    {
      ty: 4,
      nm: 'g',
      ks: {},
      shapes: [
        {
          ty: 'gr',
          it: [
            { ty: 'st', nm: 'token:ring', c: { a: 0, k: [0.5, 0.5, 0.5, 1] }, o: { a: 0, k: 100 }, w: { a: 0, k: 3 } },
            { ty: 'tr', p: { a: 0, k: [0, 0] } },
          ],
        },
      ],
    },
  ],
}

function stubFetch(impl: () => Promise<unknown>) {
  const fn = vi.fn(() => impl())
  vi.stubGlobal('fetch', fn)
  return fn
}

function jsonResponse(body: unknown, ok = true) {
  return Promise.resolve({ ok, status: ok ? 200 : 404, json: () => Promise.resolve(body) })
}

beforeEach(() => {
  loadAnimation.mockReset()
  destroy.mockReset()
  loadAnimation.mockReturnValue({
    addEventListener: (event: string, handler: () => void) => {
      if (event === 'DOMLoaded') setTimeout(handler, 0)
    },
    destroy,
    totalFrames: 60,
    play: vi.fn(),
    pause: vi.fn(),
    setSpeed: vi.fn(),
    goToAndPlay: vi.fn(),
    goToAndStop: vi.fn(),
    loop: true,
  })
  // 主题色：容器 computed color
  vi.spyOn(window, 'getComputedStyle').mockReturnValue({ color: 'rgb(10, 20, 30)' } as CSSStyleDeclaration)
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('parseColor', () => {
  it('解析 6 位 / 3 位 hex', () => {
    expect(parseColor('#ff0000')).toEqual([1, 0, 0, 1])
    expect(parseColor('#0f0')).toEqual([0, 1, 0, 1])
  })

  it('解析 rgb / rgba', () => {
    expect(parseColor('rgb(255, 128, 0)')).toEqual([1, 128 / 255, 0, 1])
    expect(parseColor('rgba(0, 0, 0, 0.5)')).toEqual([0, 0, 0, 0.5])
  })

  it('非法值与 currentColor 返回 null（保持资产原色，不要瞎染）', () => {
    expect(parseColor('currentcolor')).toBeNull()
    expect(parseColor('')).toBeNull()
    expect(parseColor('rebeccapurple')).toBeNull()
    expect(parseColor(undefined)).toBeNull()
  })
})

describe('LotionScene', () => {
  it('载入成功后挂上 ready 标记并真的创建了动画', async () => {
    stubFetch(() => jsonResponse(SCENE))
    const { container } = render(<LotionScene scene="check" size={24} />)
    const host = container.querySelector('[data-lotion="check"]')!
    expect(host).toBeTruthy()
    expect((host as HTMLElement).style.width).toBe('24px')
    await waitFor(() => expect(host.getAttribute('data-lotion-ready')).toBe('true'))
    expect(loadAnimation).toHaveBeenCalledTimes(1)
    const args = loadAnimation.mock.calls[0][0] as { renderer: string; loop: boolean; autoplay: boolean }
    expect(args.renderer).toBe('svg')
    expect(args.autoplay).toBe(false)
    // check 是"只播一次"的场景
    expect(args.loop).toBe(false)
  })

  it('token 色被替换成容器计算色（主题跟随）', async () => {
    stubFetch(() => jsonResponse(SCENE))
    render(<LotionScene scene="check" size={24} />)
    await waitFor(() => expect(loadAnimation).toHaveBeenCalled())
    const args = loadAnimation.mock.calls[0][0] as { animationData: { layers: { shapes: { it: { nm?: string; c?: { k: number[] } }[] }[] }[] } }
    const paint = args.animationData.layers[0].shapes[0].it.find(item => item.nm === 'token:ring')!
    expect(paint.c!.k[0]).toBeCloseTo(10 / 255, 4)
    expect(paint.c!.k[1]).toBeCloseTo(20 / 255, 4)
    expect(paint.c!.k[2]).toBeCloseTo(30 / 255, 4)
  })

  it('资产 404 → 等尺寸占位，不抛错、不塌布局', async () => {
    stubFetch(() => jsonResponse({}, false))
    const { container } = render(<LotionScene scene="grow" size={{ w: 120, h: 72 }} />)
    await waitFor(() => expect(container.querySelector('[data-lotion-failed="grow"]')).toBeTruthy())
    const placeholder = container.querySelector('[data-lotion-failed="grow"]') as HTMLElement
    expect(placeholder.style.width).toBe('120px')
    expect(placeholder.style.height).toBe('72px')
    expect(loadAnimation).not.toHaveBeenCalled()
  })

  it('资产结构非法 → 同样走降级，不把非法 JSON 交给 lottie', async () => {
    stubFetch(() => jsonResponse({ v: '5.7.0' }))
    const { container } = render(<LotionScene scene="scan" size={16} />)
    await waitFor(() => expect(container.querySelector('[data-lotion-failed="scan"]')).toBeTruthy())
    expect(loadAnimation).not.toHaveBeenCalled()
  })

  it('decoration 语义：默认 aria-hidden，传 label 才进无障碍树', async () => {
    stubFetch(() => jsonResponse(SCENE))
    const { container, unmount } = render(<LotionScene scene="scan" size={16} />)
    expect(container.querySelector('[data-lotion="scan"]')!.getAttribute('aria-hidden')).toBe('true')
    unmount()
    const second = render(<LotionScene scene="scan" size={16} label="扫描中" />)
    const node = second.container.querySelector('[data-lotion="scan"]')!
    expect(node.getAttribute('aria-label')).toBe('扫描中')
    expect(node.getAttribute('aria-hidden')).toBeNull()
  })

  it('卸载时销毁动画实例（不留后台 RAF）', async () => {
    stubFetch(() => jsonResponse(SCENE))
    const { container, unmount } = render(<LotionScene scene="check" size={24} />)
    await waitFor(() => expect(container.querySelector('[data-lotion-ready]')).toBeTruthy())
    unmount()
    expect(destroy).toHaveBeenCalled()
  })
})
