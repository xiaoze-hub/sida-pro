// @vitest-environment jsdom
//
// K 线画布尺寸同步回归(2026-11-17)。
//
// 事故: 图表在「容器宽度 0 / 祖先 display:none / 文档不产生渲染帧」的条件下创建后, 画布**位图**
// 停在浏览器默认的 300×150, 而元素 CSS 已是真实尺寸(实测 1096×360) —— 浏览器把 300×150
// 铺满 1096 宽, K 线变细、整幅糊化; 只能靠重建图表(周K→日K)或刷新恢复。
//
// 根因(读 lightweight-charts 5.2.1 + fancy-canvas 2.1.0 源码确认):
//   画布位图尺寸由 fancy-canvas 的 device-pixel-content-box 观察器**建议**,
//   真正写进 canvas.width/height 要等下一次绘制里的 applySuggestedBitmapSize();
//   旧实现 `chart.applyOptions({ width })` 只改元素/CSS 尺寸, 位图要等**一个渲染帧**。
//   文档不产生渲染帧时这一步永远不发生 → 位图留在 300×150。
//
// 这里钉住修复后的契约(全部走真组件代码, 只把 canvas 层换成假图表库):
//  ① 容器尺寸为 0 时**不把 0 灌进图表**(否则图表被钉在 0 宽);
//  ② 一次回调里多 entry 时用**最后一条**(浏览器会把一帧内多次变化合并, 用 [0] 会拿旧尺寸);
//  ③ 每次应用尺寸都带 forceRepaint(`resize(w, h, true)`), 位图当场跟上, 不等渲染帧;
//  ④ 位图与 CSS 脱节(width 300 / CSS 1038)时补一次 1px 抖动, 把 LWC 早退掉的那次绘制逼出来;
//  ⑤ 文档恢复可见时补同步一次(隐藏期间不会绘制);
//  ⑥ 卸载时断开观察器、摘掉监听(不留泄漏)。
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type Entry = { contentRect: { width: number; height: number } }

/** 可编程的 ResizeObserver 桩: 测试自己决定何时、用哪些 entry 触发回调。 */
class ROStub {
  static instances: ROStub[] = []
  cb: (entries: Entry[]) => void
  targets: Element[] = []
  disconnected = false
  constructor(cb: (entries: Entry[]) => void) {
    this.cb = cb
    ROStub.instances.push(this)
  }
  observe(el: Element) {
    this.targets.push(el)
  }
  unobserve() {}
  disconnect() {
    this.disconnected = true
  }
  /** 模拟浏览器: 一帧内多次变化被合并成**一次**回调、多个 entry */
  fire(widths: number[]) {
    this.cb(widths.map((w) => ({ contentRect: { width: w, height: 360 } })))
  }
}

const H = vi.hoisted(() => ({
  resizeCalls: [] as Array<[number, number, boolean | undefined]>,
}))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  API_BASE: '/api',
  fetchAPI: vi.fn(() => Promise.resolve({ klines: [] })),
}))

vi.mock('lightweight-charts', async () => {
  const stub = await import('../stubs/lightweight-charts')
  return {
    ...stub,
    // 覆写 createChart: 记录 resize 入参(本用例唯一关心的契约)
    createChart: () => {
      const chart = stub.createChart()
      return Object.assign(chart, {
        resize: (w: number, h: number, force?: boolean) => {
          H.resizeCalls.push([w, h, force])
        },
      })
    },
  }
})

import KlineChart from '@sida/biz-ui/components/KlineChart'

/** 容器 = [data-testid="kline-canvas-slot"] 里的第一个 div(组件持 ref 的那个)。 */
function container(): HTMLElement {
  const el = document.querySelector('[data-testid="kline-canvas-slot"] > div') as HTMLElement | null
  if (!el) throw new Error('未找到图表容器')
  return el
}

/** jsdom 无布局: 手动给容器一个 clientWidth。 */
function setClientWidth(el: HTMLElement, w: number) {
  Object.defineProperty(el, 'clientWidth', { value: w, configurable: true })
}

/** 往容器里塞一个"位图 300×150 / CSS 1038×332"的画布, 复现糊化态。 */
function addStaleCanvas(el: HTMLElement) {
  const c = document.createElement('canvas')
  c.width = 300
  c.height = 150
  c.getBoundingClientRect = () => ({ width: 1038, height: 332, top: 0, left: 0, right: 1038, bottom: 332, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  el.appendChild(c)
  return c
}

beforeEach(() => {
  H.resizeCalls.length = 0
  ROStub.instances.length = 0
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ROStub
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('KlineChart 画布尺寸同步', () => {
  it('容器宽 0 时绝不把 0 灌给图表(挂载期)', async () => {
    render(<KlineChart symbol="600519" market="CN" />)
    const el = container()
    setClientWidth(el, 0)
    // 触发一次观察器回调: 只有 0 宽
    await act(async () => {
      ROStub.instances.at(-1)?.fire([0])
    })
    await waitFor(() => expect(ROStub.instances.length).toBeGreaterThan(0))
    expect(H.resizeCalls.filter(([w]) => w <= 0)).toHaveLength(0)
  })

  it('一次回调里多个 entry 时取最后一条(最新尺寸), 并带 forceRepaint', async () => {
    render(<KlineChart symbol="600519" market="CN" />)
    const el = container()
    setClientWidth(el, 1096)
    await act(async () => {
      // 浏览器把 0 → 1096 合并进同一次回调: 用 entries[0] 会拿到 0(旧尺寸)
      ROStub.instances.at(-1)?.fire([0, 1096])
    })
    await waitFor(() => expect(H.resizeCalls.length).toBeGreaterThan(0))
    const applied = H.resizeCalls.map(([w, h, f]) => [w, h, f])
    expect(applied).toContainEqual([1096, 360, true])
    expect(applied.some(([w]) => (w as number) === 0)).toBe(false)
  })

  it('位图与 CSS 脱节(300×150 vs 1038×332)时补一次 1px 抖动把绘制逼出来', async () => {
    render(<KlineChart symbol="600519" market="CN" />)
    const el = container()
    setClientWidth(el, 1096)
    const c = addStaleCanvas(el)
    await act(async () => {
      ROStub.instances.at(-1)?.fire([1096])
    })
    await waitFor(() => expect(H.resizeCalls.length).toBeGreaterThanOrEqual(3))
    // 先按目标尺寸应用一次, 再 (h-1) → h 抖动: 两次都 forceRepaint, 位图当场跟上
    expect(H.resizeCalls.slice(0, 3)).toEqual([
      [1096, 360, true],
      [1096, 359, true],
      [1096, 360, true],
    ])
    // 位图已被"绘制"应用(桩里不会真画; 这里断言抖动后不再重复抖动 —— 已收敛)
    const n = H.resizeCalls.length
    await act(async () => {
      c.width = 1038
      c.height = 332
      ROStub.instances.at(-1)?.fire([1096])
    })
    expect(H.resizeCalls.length).toBe(n + 1) // 只应用一次尺寸, 不再抖动
  })

  it('文档恢复可见时补一次同步(隐藏期间不会绘制)', async () => {
    render(<KlineChart symbol="600519" market="CN" />)
    const el = container()
    setClientWidth(el, 1096)
    await act(async () => {
      ROStub.instances.at(-1)?.fire([1096])
    })
    const before = H.resizeCalls.length
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(H.resizeCalls.length).toBeGreaterThan(before)
    expect(H.resizeCalls.at(-1)).toEqual([1096, 360, true])
  })

  it('卸载: 断开观察器并摘掉 visibilitychange 监听', async () => {
    const { unmount } = render(<KlineChart symbol="600519" market="CN" />)
    const el = container()
    setClientWidth(el, 1096)
    await act(async () => {
      ROStub.instances.at(-1)?.fire([1096])
    })
    const ro = ROStub.instances.at(-1) as ROStub
    unmount()
    expect(ro.disconnected).toBe(true)
    const n = H.resizeCalls.length
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(H.resizeCalls.length).toBe(n) // 卸载后不再产生任何尺寸调用
  })

  it('环境没有 ResizeObserver: 不崩, 退回 window.resize 同步', async () => {
    // 老浏览器/受限环境: ResizeObserver 未实现时组件必须还能用窗口 resize 兜住尺寸
    delete (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver
    render(<KlineChart symbol="600519" market="CN" />)
    const el = container()
    setClientWidth(el, 1096)
    await act(async () => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(H.resizeCalls.at(-1)).toEqual([1096, 360, true])
    ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ROStub
  })
})
