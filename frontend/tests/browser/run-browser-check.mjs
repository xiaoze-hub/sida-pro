#!/usr/bin/env node
/**
 * K 线画布尺寸 — 真实浏览器用例(离线, 只连 127.0.0.1 的 fixture 页)。
 *
 * 为什么不用 vitest/jsdom: 本缺陷只在**真实轻量图表(lightweight-charts)+ 真实 ResizeObserver +
 * 真实渲染管线**里出现, jsdom 没有布局/画布位图/渲染帧, 复现不了。
 *
 * 缺点(不知道就白跑): 无渲染帧的文档(后台标签页/被遮挡窗口)是**唯一**能稳定复现的现场 ——
 * 所以用例用 CDP 把目标页留在后台(先激活另一个 target, 再从 CDP 导航), 使挂载发生在
 * `document.visibilityState === 'hidden'` 的文档里, 而浏览器不产生渲染帧。
 *
 * 用法(一条命令, 自己起/关 fixture 的 vite):
 *   node tests/browser/run-browser-check.mjs [--base=http://127.0.0.1:5199] [--shots=<dir>]
 * 若 --base 上已有 fixture 服务就直接用; 否则本脚本自己拉起
 * `vite --config tests/browser/vite.harness.config.ts`, 结束后关掉。
 *
 * 退出码: 0 全绿 / 1 有用例失败 / 2 环境错误(拿不到浏览器或页面)。
 */

import { spawn } from 'node:child_process'
import { mkdir, writeFile, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const m = /^--([^=]+)=(.*)$/.exec(a)
    return m ? [m[1], m[2]] : [a.replace(/^--/, ''), true]
  }),
)
const BASE = args.base ?? 'http://127.0.0.1:5199'
const SHOTS = args.shots ?? path.resolve(process.cwd(), '.browser-shots')
const PORT = Number(args.port ?? 9333)
/** 本脚本自己起的 vite 进程(已有服务时为 null, 由外部负责) */
let ownVite = null
/** 允许的位图/CSS 偏差(px): LWC 会把尺寸取偶数, DPR 取整也会差 1px。 */
const TOL = 2

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  path.join(os.homedir(), '.cache/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell'),
].filter(Boolean)

function findChrome() {
  for (const c of CHROME_CANDIDATES) if (existsSync(c)) return c
  return null
}

// ── 极薄 CDP 客户端(浏览器级 ws + 扁平会话) ─────────────────────
class CDP {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    this.listeners = []
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(typeof ev.data === 'string' ? ev.data : String(ev.data))
      if (msg.id !== undefined && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        msg.error ? reject(new Error(`${msg.error.message} (${msg.error.code})`)) : resolve(msg.result)
        return
      }
      for (const l of this.listeners) l(msg)
    })
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id
    const payload = { id, method, params }
    if (sessionId) payload.sessionId = sessionId
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify(payload))
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`CDP 超时: ${method}`))
      }, 30000)
    })
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitForJson(url, timeoutMs = 20000) {
  const t0 = Date.now()
  for (;;) {
    try {
      const res = await fetch(url)
      if (res.ok) return await res.json()
    } catch {
      /* 还没起来 */
    }
    if (Date.now() - t0 > timeoutMs) throw new Error(`等不到 ${url}`)
    await sleep(150)
  }
}

class Page {
  constructor(cdp, targetId, sessionId) {
    this.cdp = cdp
    this.targetId = targetId
    this.sessionId = sessionId
  }
  async eval(expr) {
    const r = await this.cdp.send(
      'Runtime.evaluate',
      { expression: expr, returnByValue: true, awaitPromise: true },
      this.sessionId,
    )
    if (r.exceptionDetails) {
      throw new Error(`页面内异常: ${r.exceptionDetails.text} ${r.exceptionDetails.exception?.description ?? ''}`)
    }
    return r.result.value
  }
  async activate() {
    await this.cdp.send('Target.activateTarget', { targetId: this.targetId })
  }
  async goto(url) {
    await this.cdp.send('Page.enable', {}, this.sessionId)
    await this.cdp.send('Page.navigate', { url }, this.sessionId)
    await sleep(250)
  }
  async screenshot(file) {
    // 后台/无帧页面上 captureScreenshot 可能超时(合成器不产帧) —— 截图是取证, 不该把用例打断
    try {
      const { data } = await this.cdp.send('Page.captureScreenshot', { format: 'png' }, this.sessionId)
      await writeFile(file, Buffer.from(data, 'base64'))
      return file
    } catch (e) {
      console.log(`  (截图跳过: ${path.basename(file)} — ${e.message})`)
      return null
    }
  }
  async close() {
    await this.cdp.send('Target.closeTarget', { targetId: this.targetId }).catch(() => {})
  }
}

/** 页内探测: 可见画布的 CSS 尺寸 vs 位图尺寸(只认有真实盒子的画布, 边轴按面积识别) */
const PROBE = `(() => {
  const host = document.getElementById('chart-host');
  const charts = host.querySelectorAll('canvas');
  const canvases = Array.from(charts).map((c) => {
    const r = c.getBoundingClientRect();
    return { cssW: Math.round(r.width), cssH: Math.round(r.height), bitmapW: c.width, bitmapH: c.height };
  }).filter((c) => c.cssW > 0 && c.cssH > 0)
    .sort((a, b) => b.cssW * b.cssH - a.cssW * a.cssH);
  const hostRect = host.getBoundingClientRect();
  const el = document.querySelector('.tv-lightweight-charts');
  return {
    vis: document.visibilityState,
    dpr: window.devicePixelRatio || 1,
    hostW: Math.round(hostRect.width),
    hostH: Math.round(hostRect.height),
    chartElW: el ? Math.round(el.getBoundingClientRect().width) : null,
    visibleCanvasCount: canvases.length,
    main: canvases[0] ?? null,
    canvases,
  };
})()`

const results = []
function record(name, ok, detail) {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  ${detail}` : ''}`)
}

/**
 * 等图表真的挂上(dev 里 KlineChart 是动态 import 的, 冷启动 transform 可能要几秒)。
 * 只等 DOM 出现, 不依赖数据加载完成 —— 位图尺寸与数据无关。
 */
async function waitForChart(page, timeoutMs = 15000) {
  const t0 = Date.now()
  for (;;) {
    const ok = await page.eval(
      `!!document.querySelector('#chart-host .tv-lightweight-charts') && document.querySelectorAll('#chart-host canvas').length > 0`,
    )
    if (ok) return true
    if (Date.now() - t0 > timeoutMs) return false
    await sleep(150)
  }
}

/** 位图必须覆盖它的 CSS 尺寸(位图 < CSS 就是被放大铺满 → 糊化) */
function bitmapCoversCss(p, tol = TOL) {
  const bad = []
  for (const c of p.canvases) {
    const needW = Math.round(c.cssW * p.dpr)
    const needH = Math.round(c.cssH * p.dpr)
    if (c.bitmapW + tol < needW || c.bitmapH + tol < needH) bad.push(c)
  }
  return bad
}

async function openCase(cdp, { url, hiddenAtMount, shots, shotPrefix }) {
  const other = await cdp.send('Target.createTarget', { url: 'about:blank' })
  const otherPage = await attach(cdp, other.targetId)
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' })
  const page = await attach(cdp, targetId)
  if (hiddenAtMount) {
    // Chrome 会把**新建**的 target 切到前台 → 建完再切回空白页, 然后从 CDP 导航,
    // 这样目标页的整个挂载过程都发生在后台(无渲染帧)。
    await otherPage.activate()
  } else {
    await page.activate()
  }
  await page.goto(url)
  const mounted = await waitForChart(page)
  if (!mounted) console.log(`  (警告: ${shotPrefix ?? url} 15s 内没等到图表挂载)`)
  await sleep(400)
  if (shotPrefix) await page.screenshot(path.join(shots, `${shotPrefix}-01-mount.png`))
  return { page, otherPage, otherTargetId: other.targetId }
}

async function attach(cdp, targetId) {
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true })
  return new Page(cdp, targetId, sessionId)
}

async function main() {
  const chrome = findChrome()
  if (!chrome) {
    console.error('找不到 Chrome/Chromium(可用 CHROME_PATH 指定)')
    process.exit(2)
  }
  // fixture 页必须先活着
  let probe = await fetch(`${BASE}/tests/browser/harness/index.html`).catch(() => null)
  if (!probe || !probe.ok) {
    console.log(`fixture 页不可达, 自己起 vite (${BASE}) …`)
    ownVite = spawn(
      path.resolve(process.cwd(), 'node_modules/.bin/vite'),
      ['--config', 'tests/browser/vite.harness.config.ts'],
      { stdio: 'ignore' },
    )
    const t0 = Date.now()
    for (;;) {
      probe = await fetch(`${BASE}/tests/browser/harness/index.html`).catch(() => null)
      if (probe && probe.ok) break
      if (Date.now() - t0 > 30000) {
        ownVite.kill('SIGKILL')
        console.error(`起不来 fixture 服务: ${BASE}/tests/browser/harness/index.html`)
        process.exit(2)
      }
      await sleep(300)
    }
  }
  await rm(SHOTS, { recursive: true, force: true })
  await mkdir(SHOTS, { recursive: true })

  const userDataDir = path.join(os.tmpdir(), `kline-size-chrome-${process.pid}`)
  const proc = spawn(
    chrome,
    [
      '--headless=new',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${userDataDir}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-gpu',
      '--window-size=1280,900',
      '--hide-scrollbars',
      'about:blank',
    ],
    { stdio: 'ignore', detached: false },
  )

  let cdp
  try {
    const version = await waitForJson(`http://127.0.0.1:${PORT}/json/version`)
    const ws = new WebSocket(version.webSocketDebuggerUrl)
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true })
      ws.addEventListener('error', rej, { once: true })
    })
    cdp = new CDP(ws)

    // ── 用例 1: 现场签名 —— 创建时容器有真实宽度, 但文档没有渲染帧(freezeRaf) ──
    // 修复前: chartEl CSS 1096, pane 画布位图停在浏览器默认 300×150 → CSS 拉伸 → 糊化
    {
      const { page } = await openCase(cdp, {
        url: `${BASE}/tests/browser/harness/index.html?target=kline&mode=plain&freezeRaf=1&case=no-frames`,
        shots: SHOTS,
        shotPrefix: 'case1-no-frames',
      })
      await sleep(800)
      const p = await page.eval(PROBE)
      const bad = bitmapCoversCss(p)
      await page.screenshot(path.join(SHOTS, 'case1-no-frames-02-after.png'))
      record(
        'case1 无渲染帧: 位图未被 CSS 放大(画布位图 ≥ CSS×DPR)',
        p.hostW === 1096 && p.visibleCanvasCount > 0 && bad.length === 0,
        `rafFrozen=${await page.eval('window.__harness.rafFrozen')} host=${p.hostW}x${p.hostH} chartEl=${p.chartElW} main=${JSON.stringify(p.main)} bad=${JSON.stringify(bad.slice(0, 2))}`,
      )
      await page.close()
    }

    // ── 用例 2: 宽 0 → 有效宽, 全程无渲染帧(观察器回调仍会到, 但 rAF 绘制不会跑) ──
    {
      const { page } = await openCase(cdp, {
        url: `${BASE}/tests/browser/harness/index.html?target=kline&mode=zero&freezeRaf=1&case=zero-to-valid`,
        shots: SHOTS,
        shotPrefix: 'case2-zero',
      })
      const before = await page.eval(PROBE)
      await page.eval('__harness.reveal()')
      await sleep(900)
      const after = await page.eval(PROBE)
      await page.screenshot(path.join(SHOTS, 'case2-zero-02-revealed.png'))
      record(
        'case2 宽0→有效(无渲染帧): 展开后位图覆盖 CSS',
        after.hostW === 1096 && after.visibleCanvasCount > 0 && bitmapCoversCss(after).length === 0,
        `before=${JSON.stringify({ hostW: before.hostW, chartElW: before.chartElW })} after=${JSON.stringify({ hostW: after.hostW, chartElW: after.chartElW, main: after.main })}`,
      )
      await page.close()
    }

    // ── 用例 3: 祖先 display:none → 显示(keepalive 面板) ───────────────────
    {
      const { page } = await openCase(cdp, {
        url: `${BASE}/tests/browser/harness/index.html?target=kline&mode=hidden&case=display-none`,
        hiddenAtMount: false,
        shots: SHOTS,
        shotPrefix: 'case3-display-none',
      })
      await page.eval('__harness.reveal()')
      await sleep(700)
      const p = await page.eval(PROBE)
      await page.screenshot(path.join(SHOTS, 'case3-display-none-02-shown.png'))
      record(
        'case3 display:none→显示: 位图覆盖 CSS + 主画布选择唯一',
        p.hostW === 1096 && p.visibleCanvasCount > 0 && bitmapCoversCss(p).length === 0,
        `vis=${p.vis} main=${JSON.stringify(p.main)} canvases=${p.visibleCanvasCount}`,
      )
      await page.close()
    }

    // ── 用例 4: 可见文档里改宽度(用户缩放/时间范围不受影响) ────────────────
    {
      const { page } = await openCase(cdp, {
        url: `${BASE}/tests/browser/harness/index.html?target=lwc&mode=plain&case=zoom`,
        hiddenAtMount: false,
        shots: SHOTS,
        shotPrefix: 'case4-zoom',
      })
      await sleep(500)
      // 先让它真的排一次版(rAF), 再设缩放 —— 否则读到的是没重算的旧区间(无帧文档的产物)
      await page.eval(`(async () => {
        const c = __harness.chart();
        c.timeScale().fitContent();
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        c.timeScale().setVisibleLogicalRange({ from: 40, to: 80 });
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        return 'ok';
      })()`)
      const zoom = await page.eval(`(() => {
        const c = __harness.chart();
        return JSON.stringify({
          range: c.timeScale().getVisibleLogicalRange(),
          barSpacing: c.timeScale().options().barSpacing,
        });
      })()`)
      // 改容器宽度 → 观察器同步(等价于"筹码面板展开挤压画布")
      await page.eval(`document.getElementById('outer').style.width = '900px'`)
      await sleep(400)
      await page.eval(`document.getElementById('outer').style.width = '1096px'`)
      await sleep(400)
      const afterZoom = await page.eval(`(() => {
        const c = __harness.chart();
        return JSON.stringify({
          range: c.timeScale().getVisibleLogicalRange(),
          barSpacing: c.timeScale().options().barSpacing,
        });
      })()`)
      const p = await page.eval(PROBE)
      await page.screenshot(path.join(SHOTS, 'case4-zoom-02-after.png'))
      const z0 = JSON.parse(zoom)
      const z1 = JSON.parse(afterZoom)
      // LWC 改宽度时保留 barSpacing(用户缩放级别)与右端锚点; 可见根数会随宽度变化 —— 这是正确行为。
      // 断言: 右端锚点不动(±1 根) 且 柱距(缩放级别) 不变。
      const driftRight = Math.abs(z0.range.to - z1.range.to)
      const driftSpacing = Math.abs(z0.barSpacing - z1.barSpacing)
      record(
        'case4 改宽度不打乱用户缩放/时间范围(柱距与右端锚点保持)',
        driftRight <= 1 && driftSpacing <= 0.02 && bitmapCoversCss(p).length === 0,
        `range ${JSON.stringify(z0.range)} → ${JSON.stringify(z1.range)} barSpacing ${z0.barSpacing}→${z1.barSpacing} driftRight=${driftRight} main=${JSON.stringify(p.main)}`,
      )
      await page.close()
    }

    // ── 用例 5: 尺寸稳定后不抖动(无无限递归/反复 resize) ─────────────────
    {
      const { page } = await openCase(cdp, {
        url: `${BASE}/tests/browser/harness/index.html?target=lwc&mode=plain&case=stable`,
        hiddenAtMount: false,
        shots: SHOTS,
        shotPrefix: 'case5-stable',
      })
      await sleep(600)
      const samples = []
      for (let i = 0; i < 8; i++) {
        samples.push(await page.eval(PROBE))
        await sleep(120)
      }
      const sig = samples.map((s) => `${s.chartElW}|${s.main?.bitmapW}x${s.main?.bitmapH}`)
      record(
        'case5 尺寸稳定后不抖动(位图不再变化)',
        new Set(sig).size === 1,
        `samples=${JSON.stringify([...new Set(sig)])}`,
      )
      await page.close()
    }

    // ── 用例 6: 真·后台标签页(不冻 rAF)加载 → 切回前台即正确(用户视角的最终保证) ──
    {
      const { page } = await openCase(cdp, {
        url: `${BASE}/tests/browser/harness/index.html?target=kline&mode=zero&case=background-tab`,
        hiddenAtMount: true,
        shots: SHOTS,
        shotPrefix: 'case6-background-tab',
      })
      const hiddenState = await page.eval(PROBE)
      // 后台把容器展开(keepalive 面板预渲染的等价动作), 此时没有渲染帧
      await page.eval('__harness.reveal()')
      await sleep(400)
      // 切回前台(= 用户点开这个标签)
      await page.activate()
      await sleep(800)
      const p = await page.eval(PROBE)
      await page.screenshot(path.join(SHOTS, 'case6-background-tab-03-focused.png'))
      record(
        'case6 后台标签页加载后切回前台: 位图覆盖 CSS',
        p.vis === 'visible' && p.hostW === 1096 && bitmapCoversCss(p).length === 0,
        `挂载时 vis=${hiddenState.vis} → 切回后 vis=${p.vis} host=${p.hostW} main=${JSON.stringify(p.main)}`,
      )
      await page.close()
    }
  } finally {
    try {
      proc.kill('SIGKILL')
    } catch {
      /* ignore */
    }
    try {
      ownVite?.kill('SIGKILL')
    } catch {
      /* ignore */
    }
    await rm(userDataDir, { recursive: true, force: true }).catch(() => {})
  }

  const failed = results.filter((r) => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} 通过, 截图: ${SHOTS}`)
  process.exit(failed.length ? 1 : 0)
}

main().catch((e) => {
  console.error('环境错误:', e?.message ?? e)
  process.exit(2)
})
