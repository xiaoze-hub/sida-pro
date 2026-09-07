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
 */
import { readFileSync, readdirSync, statSync } from 'fs'
import { join, extname } from 'path'

const ROOT = new URL('..', import.meta.url).pathname
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
    // R4 (令牌定义文件本身豁免: hex 只许出现在 fallback 默认值里)
    if (
      !/lib\/stock-colors\.ts$/.test(f) &&
      !R4_SKIP_LINE.some((p) => p.test(ln)) &&
      /Kline|GsColors|stock-colors|GsSignal|gs/i.test(f) &&
      /#E53935|#43A047|#ef4444|#22c55e|#EF4444|#22C55E/i.test(ln)
    )
      bad('R4-NO-HARDCODED-GS-COLOR', rel(f), n, ln)
  })
}

console.log(fails === 0 ? 'UI-RULES OK' : `UI-RULES FAIL: ${fails} violation(s)`)
process.exit(fails === 0 ? 0 : 1)
