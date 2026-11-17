/**
 * 真实浏览器 fixture(仅 127.0.0.1): 复现「图表初始容器宽 0 / 祖先 display:none / 后台标签页
 * (无渲染帧)」时 lightweight-charts 画布位图尺寸不跟容器的问题。
 *
 * 用法: /tests/browser/harness/index.html?target=kline&mode=zero|hidden
 *   target=kline : 真 KlineChart 组件(走组件的 createChart + ResizeObserver 路径)
 *   target=lwc   : 最小对照(裸 createChart, 与组件同样的 createChart({width: clientWidth}) + 同款 RO)
 *
 * 挂载后不自动展开容器; 由驱动脚本(或手工 __harness.reveal())展开, 使「隐藏挂载 → 展开」时序确定,
 * 不依赖网络/数据加载速度。
 */

import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
import { createChart as createLwcChart, CandlestickSeries } from 'lightweight-charts'

const qs = new URLSearchParams(location.search)
const target = qs.get('target') === 'lwc' ? 'lwc' : 'kline'
const mode = qs.get('mode') === 'hidden' ? 'hidden' : qs.get('mode') === 'plain' ? 'plain' : 'zero'
document.body.classList.add(`mode-${mode}`)

/**
 * freezeRaf=1: 冻结渲染帧 —— 复现「文档不产生渲染帧」(后台标签页/被遮挡窗口/省电节流)。
 *
 * 为什么不靠真实后台标签页: 后台标签页确实没有渲染帧(实测手工复现过 300×150), 但
 * ①CDP 截图在无帧页面上会超时 ②"页面第一次 commit 时有没有偷偷画一帧"不受用例控制 → 不确定。
 * 把 requestAnimationFrame 换成空实现是**忠实且确定**的等价物: 图表库的画布位图应用
 * (fancy-canvas 的 applySuggestedBitmapSize)正是排在 rAF 绘制里的。
 */
const freezeRaf = qs.get('freezeRaf') === '1'
if (freezeRaf) {
  let seq = 1
  window.requestAnimationFrame = ((() => seq++) as unknown) as typeof window.requestAnimationFrame
  window.cancelAnimationFrame = (() => {}) as typeof window.cancelAnimationFrame
}

const host = document.getElementById('chart-host') as HTMLDivElement
const outer = document.getElementById('outer') as HTMLDivElement

// ── 合成数据(唯一数据源, 不出本机) ───────────────────────────────
function syntheticKlines(n = 120) {
  const out: Array<Record<string, unknown>> = []
  let price = 100
  const start = new Date('2026-01-05T00:00:00Z')
  for (let i = 0; i < n; i++) {
    const d = new Date(start.getTime() + i * 86400000)
    const wd = d.getUTCDay()
    if (wd === 0 || wd === 6) continue // 日 K 序列里没有周末
    price *= 1 + (Math.sin(i / 5) * 0.012 + 0.0008)
    const open = price
    const close = price * (1 + Math.sin(i / 3) * 0.006)
    out.push({
      date: d.toISOString().slice(0, 10),
      open: +open.toFixed(2),
      high: +(Math.max(open, close) * 1.008).toFixed(2),
      low: +(Math.min(open, close) * 0.992).toFixed(2),
      close: +close.toFixed(2),
      volume: 1_000_000 + i * 1000,
    })
  }
  return out
}

const KLINES = syntheticKlines()

// fetch 全量桩: 只有 /klines/{symbol}(列表) 有数据, 其余图层端点回空(组件按空处理, 不报错)
window.fetch = (async (input: RequestInfo | URL) => {
  const url = typeof input === 'string' ? input : String((input as Request).url ?? input)
  let data: unknown = {}
  if (/\/klines\/[^/?]+(\?.*)?$/.test(url)) {
    data = { klines: KLINES, live_bar: null, synthetic: true }
  }
  return new Response(JSON.stringify({ code: 0, success: true, data, message: 'ok', synthetic: true }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}) as typeof fetch

// ── 画布探测: 只认"可见的目标主画布"(多 canvas 的边轴/时间轴不能误判) ──────
type CanvasProbe = {
  cssW: number
  cssH: number
  bitmapW: number
  bitmapH: number
  dpr: number
  area: number
}
function visibleCanvases(): CanvasProbe[] {
  const all = Array.from(host.querySelectorAll('canvas')) as HTMLCanvasElement[]
  const out: CanvasProbe[] = []
  for (const c of all) {
    const r = c.getBoundingClientRect()
    // 不可见(无盒子/0 面积)的 canvas 排除: 隐藏 keepalive、折叠轴、离屏 buffer 都不参与判定
    if (r.width <= 0 || r.height <= 0) continue
    if (c.offsetParent === null) continue
    out.push({
      cssW: Math.round(r.width * 100) / 100,
      cssH: Math.round(r.height * 100) / 100,
      bitmapW: c.width,
      bitmapH: c.height,
      dpr: window.devicePixelRatio || 1,
      area: r.width * r.height,
    })
  }
  // 面积从大到小: [0] 就是主图 pane 画布(边轴都是窄条), 边轴不会被选中
  return out.sort((a, b) => b.area - a.area)
}

function measure() {
  const canvases = visibleCanvases()
  const main = canvases[0] ?? null
  const hostRect = host.getBoundingClientRect()
  return {
    hostRect: {
      w: Math.round(hostRect.width * 100) / 100,
      h: Math.round(hostRect.height * 100) / 100,
    },
    visibleCanvasCount: canvases.length,
    main,
    canvases: canvases.slice(0, 4),
    // 期望的位图宽: 主画布 CSS 宽 × DPR(±2px 取整容差)
    expected: main ? Math.round(main.cssW * main.dpr) : null,
  }
}

/** 直接读 DOM 上的 canvas 位图(不受"可见"过滤限制), 用于暴露隐藏态下的真实位图 */
function rawCanvases() {
  return Array.from(host.querySelectorAll('canvas')).map((c) => {
    const el = c as HTMLCanvasElement
    const r = el.getBoundingClientRect()
    return [el.width, el.height, Math.round(r.width), Math.round(r.height)]
  })
}

function reveal() {
  if (mode === 'hidden') outer.style.display = 'block'
  else if (mode === 'zero') outer.style.width = '1096px'
  document.body.dataset.revealed = '1'
}

// ── 挂载 ──────────────────────────────────────────────────────
let dispose: (() => void) | null = null
let chartHandle: unknown = null

async function mount() {
  if (target === 'lwc') {
    // 对照: 与 KlineChart 同款创建方式(width: container.clientWidth) + 同款 ResizeObserver
    const chart = createLwcChart(host, { width: host.clientWidth, height: 360 })
    const s = chart.addSeries(CandlestickSeries, {})
    s.setData(
      KLINES.map((k) => ({
        time: k.date as string,
        open: k.open as number,
        high: k.high as number,
        low: k.low as number,
        close: k.close as number,
      })),
    )
    const ro = new ResizeObserver((entries) => {
      const e = entries[0]
      if (e) chart.applyOptions({ width: e.contentRect.width })
    })
    ro.observe(host)
    chartHandle = chart
    dispose = () => {
      ro.disconnect()
      chart.remove()
    }
  } else {
    const mod = await import('../../../packages/biz-ui/src/components/KlineChart')
    const KlineChart = mod.default
    const root = createRoot(host)
    root.render(createElement(KlineChart, { symbol: '600519', market: 'CN', initialDays: 120 }))
    dispose = () => root.unmount()
  }
  await new Promise((r) => setTimeout(r, 60))
}

const ready = mount().then(() => ({ ok: true, target, mode }))

;(window as unknown as Record<string, unknown>).__harness = {
  ready,
  reveal,
  measure,
  rawCanvases,
  chart: () => chartHandle,
  hostSize: () => ({ w: host.clientWidth, h: host.clientHeight }),
  vis: () => document.visibilityState,
  rafFrozen: freezeRaf,
  unmount: () => dispose?.(),
  canvases: () => visibleCanvases(),
}
