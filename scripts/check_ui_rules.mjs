#!/usr/bin/env node
/**
 * UI 铁律门禁 (2026-09-07 P3, 零依赖纯 node, 给 CI 用: node scripts/check_ui_rules.mjs)。
 * 失败 exit 1 并打印违规文件:行。
 *
 * R1 chart 图层禁卡片: KlineChart/InteractiveKline 内禁 import Card* / ContextCard
 *    (K线是绝对主角, hairline 分隔取代边框 — 产品定位铁律)。
 * R2 禁手抄 toAmount: pages 下禁本地 function/const toAmount, 一律走 @/lib/format。
 * R3 禁 CRLF: src 全仓 .ts/.tsx 不许含 \r。
 * R4 GS 颜色禁硬编码: chart 相关文件禁 #E53935/#43A047/#ef4444/#22c55e 直写,
 *    一律走 readGsColors()/--gs-go/--gs-stop 令牌(买红卖绿, override 设计稿§5.2)。
 * R5 API_BASE 已含 '/api', 字符串再写 '/api/...' 即双前缀死链。
 * R6 toFixed 棘轮(2026-09-09 E3): 裸 .toFixed( 对字符串数字会崩(PG DECIMAL→JSON 是
 *    字符串, 2026-08-21 c.price.toFixed 事故)。存量冻结在 scripts/ui-rules-baseline.json,
 *    只许降不许升; 新文件出现 .toFixed( 直接失败。新代码一律走 @/lib/format safe* 系列。
 * R7 禁 null→0 三元(2026-09-09 E3): `x == null ? 0 : ...` 会把缺数据渲染成 "+0.00%"
 *    掩盖缺失, 应走 safeNum()/safePercent() 的 '--' fallback。
 * R8 固定底部条必须预留空间(2026-09-18 UI 走查): `fixed inset-x-0 bottom*` 的常驻条会压住
 *    页面最后一行(走查 9 页复现)。该文件必须同时出现 `--disclaimer-h`(或自行补偿 padding),
 *    否则 CI 失败 —— 防止以后再加一条固定条重犯。
 * R11 只允许两个 K 线引导模块(2026-09-18 K 线引擎合并 P3): 全仓 `createChart(` 只许出现在
 *    `KlineChart.tsx` / `MinuteLwcChart.tsx`。历史上 IK/KC/MinuteLwcChart 三套各自建图,
 *    任何全局能力(图表库升级、数据源裁决、成交标记)都要改三遍 —— 这条门禁把它锁死为单核。
 * R10 字阶棘轮(2026-09-18 UI 走查 B4): 全站曾出现 12 种 px 字号(9/10/11/12/13/14/15/16/17/18/20/22),
 *    "层级平淡"其实是"级数失控"。规范只留 6 级: 10 辅助 / 11 次要 / 12 正文 / 13 区块标题 /
 *    16 页面标题 / 20 大数字。**已有文件按 baseline 只降不升**, 新文件一律 0(基线见
 *    scripts/ui-rules-font-baseline.json)。
 * R10b 10px 棘轮(2026-10-24 G1 第 1 步): 全仓 `text-[10px]` 730 处, 不能一次收敛。
 *    按**文件**冻结存量, 只减不增(基线 scripts/ui-rules-font10-baseline.json,
 *    由 `--write-font10-baseline` 生成)。10px 收敛到 0 后才允许从 FONT_ALLOWED 移除 10。
 * R9 禁外部 CDN 脚本(2026-09-18 UI 走查): index.html 不许再引 unpkg/jsdelivr 之类外部域。
 *    生产实测 CSP/国内网络任一环节拦掉 CDN, 图表库直接加载失败、主视觉区空白。
 *    图表库已随 biz-ui 依赖打包(localhost 自托管), 无需 CDN。
 */
import { readFileSync, readdirSync, statSync, writeFileSync } from 'fs'
import { join, extname } from 'path'
import { fileURLToPath } from 'url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC = join(ROOT, 'frontend', 'src')
const PKG_BIZ = join(ROOT, 'frontend', 'packages', 'biz-ui', 'src')

let fails = 0
const bad = (rule, file, line, hit) => {
  fails++
  console.log(`[${rule}] ${file}:${line}: ${hit.trim().slice(0, 100)}`)
}

function walk(dir, out = []) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e)
    if (statSync(p).isDirectory()) out.push(...walk(p, []))
    else if (['.ts', '.tsx'].includes(extname(p))) out.push(p)
  }
  return out
}

const rel = (p) => p.replace(ROOT, '')

// R3 先行: 读文件即检 CRLF
for (const f of [...walk(SRC), ...walk(PKG_BIZ)]) {
  const buf = readFileSync(f)
  if (buf.includes('\r')) bad('R3-CRLF', rel(f), 1, 'file contains CR')
}
const files = [...walk(SRC), ...walk(PKG_BIZ)].filter((f) => !readFileSync(f).includes('\r'))

// R7 豁免清单(带理由, 见循环内注释)
const R7_SKIP_LINE = [/macdVals\s*=\s*macd\.map/, /difVals\s*=\s*dif\.map/]

const CHART = ['KlineChart.tsx', 'InteractiveKline.tsx']
// R1 白名单(2026-09-07 P3 实测定级): InteractiveKline 分时模式下挂在图下方的堆叠
// 明细面板，非包裹/覆盖 K 线主角的卡片化。挪动它们的位置时重审此条。
const R1_ALLOW = [
  { file: 'InteractiveKline.tsx', pat: /import\s+DarkFlowCards\s+from/ },
  { file: 'InteractiveKline.tsx', pat: /import\s+AuctionSnapshotCard\s+from/ },
]
// R4 豁免(2026-09-07): split_cluster 拆股 marker 绿是事件色板语义，非 GS 买卖信号。
// (case 标签与 marker 行分离，exempt 匹配 marker 文本 `拆${`。)
const R4_SKIP_LINE = [/split_cluster/, /拆\$\{/]
for (const f of files) {
  const lines = readFileSync(f, 'utf8').split('\n')
  const name = f.split('/').pop()
  lines.forEach((ln, i) => {
    const n = i + 1
    // R1
    if (CHART.includes(name) && /import\s+.*(Card|ContextCard)/.test(ln) && !ln.trim().startsWith('//')) {
      const allowed = R1_ALLOW.some((a) => a.file === name && a.pat.test(ln))
      if (!allowed) bad('R1-NO-CARD-IN-CHART', rel(f), n, ln)
    }
    // R2: 本地 toAmount 定义(调用点不管)
    if (/^\s*(function\s+toAmount|const\s+toAmount\s*=)/.test(ln)) bad('R2-NO-LOCAL-toAmount', rel(f), n, ln)
    // R4 (令牌定义文件本身豁免: hex 只许出现在 fallback 默认值里; 路径分隔符归一化, Windows 反斜杠也能命中)
    if (
      !/lib\/stock-colors\.ts$/.test(f.replace(/\\/g, '/')) &&
      !R4_SKIP_LINE.some((p) => p.test(ln)) &&
      // 判据用**文件名**(而非全路径): 全路径会误伤 checkout 目录名含 "gs" 的情况
      // (worktree /home/ubuntu/wt/gs-fund-layer 下, 无辜的 SentimentGauge.tsx 会被当成 GS 文件)。
      /Kline|GsColors|stock-colors|GsSignal|gs/i.test(f.split('/').pop() ?? f) &&
      /#E53935|#43A047|#ef4444|#22c55e|#EF4444|#22C55E/i.test(ln)
    )
      bad('R4-NO-HARDCODED-GS-COLOR', rel(f), n, ln)
    // R5: API_BASE 已含 '/api', 字符串再写 '/api/...' 即双前缀死链
    if (/['"`]\/api\/api\//.test(ln)) bad('R5-NO-DOUBLE-API-PREFIX', rel(f), n, ln)
    // R7: null/undefined 三元落到 0 —— 缺数据被渲染成 "+0.00%" 掩盖缺失, 应走 '--'
    // 豁免(2026-09-09): `macdVals = macd.map(v => v == null ? 0 : v)` 是喂 ema() 的暖机
    // 种子(仅前 26 根窗口), 数学输入而非渲染; 渲染侧 null 由 hist 的 null 守卫处理。
    // 豁免(2026-09-10): `difVals = dif.map(v => v == null ? 0 : v)` 同类——DIF 暖机期
    // null 喂 dea=emaSeries(difVals), 渲染侧 hist 对 dif/dea 双 null 守卫。
    if (!R7_SKIP_LINE.some((p) => p.test(ln)) && /(==|===)\s*(null|undefined)\s*\?\s*0\s*:/.test(ln)) bad('R7-NO-NULL-ZERO-COERCE', rel(f), n, ln)
  })
}

// R6: toFixed 棘轮(只许降不许升, 新文件直接失败)
const BASELINE_FILE = join(ROOT, 'scripts', 'ui-rules-baseline.json')
let baseline = {}
try { baseline = JSON.parse(readFileSync(BASELINE_FILE, 'utf8')) } catch { /* 无 baseline 视为全量新规 */ }
const normKey = (f) => f.replace(/\\/g, '/').replace(ROOT.replace(/\\/g, '/'), '')
const seen = new Set()
for (const f of files) {
  const c = (readFileSync(f, 'utf8').match(/\.toFixed\(/g) || []).length
  if (c === 0) continue
  const key = normKey(f)
  seen.add(key)
  const base = baseline[key]
  if (base === undefined) bad('R6-TOFIXED-RATCHET', rel(f), 1, `${c} toFixed in file without baseline (use @/lib/format safe*)`)
  else if (c > base) bad('R6-TOFIXED-RATCHET', rel(f), 1, `${c} toFixed > baseline ${base}`)
  else if (c < base) console.log(`[R6] ${key}: ${c} < baseline ${base} — 可调低 baseline`)
}
for (const k of Object.keys(baseline)) if (!seen.has(k)) console.log(`[R6] ${k}: 0 — 可从 baseline 删除`)

// R8: 固定底部条必须预留空间(见文件头 R8 说明)
for (const f of files) {
  const lines = readFileSync(f, 'utf8').split(/\r?\n/)
  const hitIdx = lines.findIndex((ln) => /fixed[^"'`]*inset-x-0[^"'`]*bottom/.test(ln))
  if (hitIdx < 0) continue
  const whole = lines.join('\n')
  // 预留机制二选一: 直接引用高度变量(--disclaimer-h), 或声明占用标记(has-disclaimer)
  if (!whole.includes('disclaimer-h') && !whole.includes('has-disclaimer')) {
    bad('R8-FIXED-BOTTOM-NO-RESERVE', rel(f), hitIdx + 1,
        'fixed bottom bar 未为自身预留空间: 请给内容区加 pb-[var(--disclaimer-h)] 补偿(见 index.css)')
  }
}

// R9: index.html 禁外部 CDN 脚本
try {
  const html = readFileSync(join(ROOT, 'frontend', 'index.html'), 'utf8')
  const cdn = html.match(/(?:src|href)=["']https?:\/\/(?:unpkg\.com|cdn\.jsdelivr\.net)[^"']*/g) || []
  for (const c of cdn) bad('R9-EXTERNAL-CDN', 'frontend/index.html', 1, c)
} catch { /* index.html 不存在则跳过 */ }

// R10: 字阶棘轮(见文件头说明)
const FONT_ALLOWED = new Set([10, 11, 12, 13, 16, 20])
// 公开面(官网/开发者文档)是营销与文档场景, 允许比终端宽的字阶 —— 上限 7 档
// (设计稿 v3.0 §三 3.2)。判定按**文件**分流, 不是放宽终端口径。
const FONT_ALLOWED_PUBLIC = new Set([12, 13, 16, 20, 28, 36, 48])
const PUBLIC_SURFACE = [
  'frontend/src/pages/Landing.tsx',
  'frontend/src/pages/Developers.tsx',
  // P2-4(2026-09-18): 档位页是**公开面**(未登录可见, 营销/转化场景), 与落地页同档位口径。
  'frontend/src/pages/Tiers.tsx',
]
const allowedFor = (key) => (PUBLIC_SURFACE.includes(key) ? FONT_ALLOWED_PUBLIC : FONT_ALLOWED)
const FONT_BASELINE_FILE = join(ROOT, 'scripts', 'ui-rules-font-baseline.json')
let fontBaseline = {}
try { fontBaseline = JSON.parse(readFileSync(FONT_BASELINE_FILE, 'utf8')) } catch { /* 无基线 → 全量新规 */ }
const fontSeen = new Set()
for (const f of files) {
  const src = readFileSync(f, 'utf8')
  // 注意: 变量名不能叫 bad —— 会遮蔽上报函数 bad()
  const key = f.replace(/\\/g, '/').split('/frontend/')[1] || rel(f)
  const allowed = allowedFor(`frontend/${key}`)
  // 2026-09-18 补两个盲区(实测踩到): ① Tailwind 命名字号(text-sm=14/lg=18/2xl=24/3xl=30);
  // ② JSX 内联 SVG 字号 `fontSize={N}`(注意: ECharts 选项里的 `fontSize: N` **不拦** ——
  // 那是画布内文字, 不参与 DOM 字阶体系)。
  const named = (src.match(/(?<![-\w])text-(sm|lg|2xl|3xl|4xl|5xl)(?![-\w])/g) || []);
  const inline = (src.match(/fontSize=\{(\d+)\}/g) || [])
    .map((m) => Number(/(\d+)/.exec(m)[1]))
    .filter((n) => !allowed.has(n));
  const badSizes = (src.match(/text-\[(\d+)px\]/g) || [])
    .map((m) => Number(/(\d+)/.exec(m)[1]))
    .filter((n) => !allowed.has(n))
    .concat(named.map(() => -1))
    .concat(inline)
  if (badSizes.length === 0) continue
  fontSeen.add(key)
  const base = fontBaseline[key]
  const where = `非标准字号 ${[...new Set(badSizes)].join(',')}px (允许 ${[...allowed].join('/')})`
  if (base === undefined) {
    bad('R10-FONT-SCALE', rel(f), 1, `${badSizes.length} 处${where} 且无 baseline`)
  } else if (badSizes.length > base) {
    bad('R10-FONT-SCALE', rel(f), 1, `${badSizes.length} > baseline ${base} — ${where}`)
  } else if (badSizes.length < base) {
    console.log(`[R10] ${key}: ${badSizes.length} < baseline ${base} — 可调低 baseline`)
  }
}
for (const k of Object.keys(fontBaseline)) if (!fontSeen.has(k)) console.log(`[R10] ${k}: 0 — 可从 baseline 删除`)

// ── R10b: 10px 棘轮(2026-10-24, G1 字号收敛第 1 步)────────────────────────────
// 背景: 审计 G1 要把字号收敛到 {12,13,16,28}, 但全仓 `text-[10px]` 有 730 处 ——
// 一次改完无法目检。改为分三步: ①先按**文件**冻结存量(本条), ②各页 lane 逐页收敛并调低,
// ③全仓归零后才动 FONT_ALLOWED。
// 与 R10 职责分离: R10 管「有没有非法档位」, R10b 管「10px 有没有变多」。
// 基线由 `node scripts/check_ui_rules.mjs --write-font10-baseline` 生成, 不许手敲。
{
  const F10_BASELINE_FILE = join(ROOT, 'scripts', 'ui-rules-font10-baseline.json')
  const WRITE_F10 = process.argv.includes('--write-font10-baseline')
  let f10 = {}
  try { f10 = JSON.parse(readFileSync(F10_BASELINE_FILE, 'utf8')) } catch { /* 无基线 → 全量新规 */ }
  const counts = {}
  for (const f of files) {
    const c = (readFileSync(f, 'utf8').match(/text-\[10px\]/g) || []).length
    if (c === 0) continue
    const key = f.replace(/\\/g, '/').split('/frontend/')[1] || rel(f)
    counts[key] = c
  }
  if (WRITE_F10) {
    const total = Object.values(counts).reduce((a, b) => a + b, 0)
    const payload = {
      _note: '2026-10-24 建立(G1 第 1 步)。规则: 每文件 text-[10px] 只减不增; 收敛到 0 的文件从本表删除。'
        + ' 全仓归零后才允许把 FONT_ALLOWED 的 10 移除。本文件由 node scripts/check_ui_rules.mjs --write-font10-baseline 生成, 不许手改数字。',
      _total: total,
      ...counts,
    }
    writeFileSync(F10_BASELINE_FILE, `${JSON.stringify(payload, null, 2)}\n`)
    console.log(`[R10b] baseline written: ${Object.keys(counts).length} files / ${total} 处 text-[10px]`)
    process.exit(0)
  }
  const f10Seen = new Set()
  for (const [key, c] of Object.entries(counts)) {
    f10Seen.add(key)
    const base = f10[key]
    if (base === undefined) {
      bad('R10b-FONT-10-RATCHET', key, 1, `${c} 处 text-[10px] 且无 baseline —— 新代码从 12px 起, 见 docs/design 勘误 7.1`)
    } else if (c > base) {
      bad('R10b-FONT-10-RATCHET', key, 1, `${c} 处 10px > baseline ${base} —— 只减不增; 需要收敛请用 12px`)
    } else if (c < base) {
      console.log(`[R10b] ${key}: ${c} < baseline ${base} — 可调低 baseline`)
    }
  }
  for (const k of Object.keys(f10)) {
    if (k.startsWith('_')) continue
    if (!f10Seen.has(k)) console.log(`[R10b] ${k}: 0 — 可从 baseline 删除`)
  }
}

// R11: 只允许两个 K 线引导模块(见文件头 R11 说明)
{
  const ALLOWED_BOOTSTRAP = new Set([
    'frontend/packages/biz-ui/src/components/KlineChart.tsx',
    'frontend/packages/biz-ui/src/components/MinuteLwcChart.tsx',
  ])
  for (const f of files) {
    const src = readFileSync(f, 'utf8')
    if (!/\bcreateChart\s*\(/.test(src)) continue
    const key = normKey(f)
    if (!ALLOWED_BOOTSTRAP.has(key)) {
      bad('R11-MULTIPLE-CHART-BOOTSTRAP', rel(f), 1,
          `createChart 只允许出现在 ${[...ALLOWED_BOOTSTRAP].map((x) => x.split('/').pop()).join(' / ')}`)
    }
  }
}

// ── R12: Surface 棘轮(2026-09-20 设计系统落地)────────────────────────────────
// 依据: 全站曾散落 bg-accent/xx 300+ 处、bg-white/[0.0x] 30 处 —— 同一视觉层级靠手调透明度,
// 结果"同层不同色"。新代码一律走 bg-s0..s4 / bg-canvas(阶梯 token), 存量**只减不增**。
// `bg-stock-up/down` 作**背景**同属此列: 背景只表达空间与会话, 涨跌用文字/图形表达。
{
  const SURFACE_BASELINE_FILE = join(ROOT, 'scripts', 'ui-rules-surface-baseline.json')
  let sb = {}
  try { sb = JSON.parse(readFileSync(SURFACE_BASELINE_FILE, 'utf8')) } catch { /* 无基线 → 全量新规 */ }
  const sources = files.map((f) => readFileSync(f, 'utf8')).join('\n')
  const now = {
    bare_alpha: (sources.match(/bg-white\/\[/g) || []).length,
    accent_alpha: (sources.match(/bg-accent\/[0-9]/g) || []).length,
    price_bg: (sources.match(/bg-stock-(up|down)/g) || []).length,
  }
  for (const [k, v] of Object.entries(now)) {
    const b = sb[k]
    if (b === undefined) continue
    if (v > b) bad('R12-SURFACE-RATCHET', 'frontend', 1, `${k}: ${v} > baseline ${b} — 新增背景请用 bg-s0..s4 / bg-canvas, 不许再手调透明度或背景上色`)
    else if (v < b) console.log(`[R12] ${k}: ${v} < baseline ${b} — 可调低 baseline`)
  }
}

// ── R13: 单一浮层层级 + 圆角上限(2026-09-20)────────────────────────────────
// 依据: 实测 shadow-lg/xl/2xl/md 混用 18 处、rounded-2xl 14 处 —— 层级靠阴影大小猜。
// 平铺面板一律 hairline 边框; 真正的浮层只允许 `.shadow-float` 一档; 圆角上限 rounded-xl(由 --radius 派生)。
{
  for (const f of files) {
    const src = readFileSync(f, 'utf8')
    const sh = (src.match(/\bshadow-(2xl|xl|lg|md)\b/g) || [])
    const rd = (src.match(/\brounded-(2xl|3xl)\b/g) || [])
    if (sh.length) bad('R13-ELEVATION', rel(f), sh.length, `shadow-${sh[0].split('-')[1]} —— 浮层只用 .shadow-float, 平铺面板用 hairline 边框`)
    if (rd.length) bad('R13-ELEVATION', rel(f), rd.length, `${rd[0]} —— 圆角上限 rounded-xl(随 --radius)`)
  }
}

// ── R14: 文字禁止用透明度做层级(2026-10-24)──────────────────────────────
// 依据: 全站 313 处 text-*\/NN（text-muted-foreground/70 就有 130 处）。暗色次级色本身
// 是 4.92:1（刚过 AA），叠 70% alpha 后实测掉到 **2.22:1** —— 侧栏分组标题「行情/机会/决策」
// 就是这么变灰的，而它在肉眼上只像"稍微淡一点"，不会有人报 bug。
// 层级用**字号/字重**表达；有色底（primary/white/destructive）上的 alpha 不在此列。
{
  for (const f of files) {
    const src = readFileSync(f, 'utf8')
    const hits = (src.match(/\btext-(?:muted-)?foreground\/(?:[1-9][0-9]?)\b/g) || [])
    if (hits.length) {
      bad(
        'R14-TEXT-ALPHA',
        rel(f),
        hits.length,
        `${hits[0]} —— 文字不要用透明度降级(实测 2.22:1, AA 需 4.5); 更弱的层级用字号/字重`,
      )
    }
  }
}

console.log(fails === 0 ? 'UI-RULES OK' : `UI-RULES FAIL: ${fails} violation(s)`)
process.exit(fails === 0 ? 0 : 1)
