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
 */
import { readFileSync, readdirSync, statSync } from 'fs'
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
      /Kline|GsColors|stock-colors|GsSignal|gs/i.test(f) &&
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

console.log(fails === 0 ? 'UI-RULES OK' : `UI-RULES FAIL: ${fails} violation(s)`)
process.exit(fails === 0 ? 0 : 1)
