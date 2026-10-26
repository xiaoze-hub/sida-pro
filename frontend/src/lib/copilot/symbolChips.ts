/**
 * 从 AI 回答正文里抽 6 位 A 股代码 → 可点击跳工作台。
 *
 * 纯函数，单测覆盖两类误判：
 *  - 日期（2026-10-18 的 20261018 不是 6 位；但 "2026年10月18日" 里的 202610 也不能当代码）；
 *  - 金额 / 数量（120000 手、350000 万元）。
 * 做法：要求代码左右**不是数字**，且不能紧跟「年/月/日/手/万/元/%」这类量纲字。
 */
const CODE_RE = /(?:^|\D)([034689]\d{5})(?!\d)/g
const UNIT_AFTER = /^[\s]*(?:年|月|日|手|万|亿|元|%|股|倍|点)/

export function extractSymbolChips(text: string, limit = 6): string[] {
  if (!text) return []
  const out: string[] = []
  for (const m of text.matchAll(CODE_RE)) {
    const code = m[1]
    const idx = m.index ?? 0
    const tail = text.slice(idx + m[0].length, idx + m[0].length + 3)
    if (UNIT_AFTER.test(tail)) continue
    if (!out.includes(code)) out.push(code)
    if (out.length >= limit) break
  }
  return out
}

/** 6 位代码 → 市场后缀（后端口径 SH/SZ/BJ，与 tq 的 6→SH / 0,3→SZ / 4,8,9→BJ 一致）。 */
export function marketOf(code: string): 'SH' | 'SZ' | 'BJ' {
  const c = (code || '').trim()
  if (c.startsWith('6')) return 'SH'
  if (/^[489]/.test(c)) return 'BJ'
  return 'SZ'
}
