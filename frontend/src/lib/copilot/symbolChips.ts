/**
 * 从 AI 回答正文里抽 6 位 A 股代码 → 可点击跳工作台。
 *
 * 纯函数，单测覆盖两类误判：
 *  - 日期（2026-10-18 的 20261018 不是 6 位；但 "2026年10月18日" 里的 202610 也不能当代码）；
 *  - 金额 / 数量（120000 手、350000 万元）。
 * 做法：要求代码左右不是字母、数字或下划线（允许 SH/SZ/BJ 市场前缀），且不能紧跟「年/月/日/手/万/元/%」这类量纲字。
 */
const CODE_RE = /(?:^|[^A-Za-z0-9_])(?:SH|SZ|BJ)?([034689]\d{5})(?![A-Za-z0-9_])/gi
const UNIT_AFTER = /^[\s]*(?:年|月|日|手|万|亿|元|%|股|倍|点)/

export function extractSymbolChips(text: string, limit = 6): string[] {
  if (!text || /^\s*(?:抱歉，)?AI 服务暂时不可用[：:]/.test(text)) return []
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

/** 本轮提问指向的标的 + 这个指向是不是用户**自己写明的**（可证）。 */
export interface TurnSymbol {
  symbol: string
  /**
   * `true` = 用户在正文里写了代码，或 `@` 选了具体票（可证）；
   * `false` = 只是沿用页面锚点/会话绑定（**推定**，不是这一轮写明的）。
   */
  explicit: boolean
}

/**
 * 本轮标的解析（2026-10-29 三审）。
 *
 * 早先后台轮只从正文抽 6 位代码（`extractSymbolChips`），于是「@ 选了宁德时代」「只说名字不说代码」
 * 都判不出换票，旧会话（创建时带着另一只票的图表快照）被继续复用。
 * 这里把三种来源排成一条可信度链：@ 指定 > 正文代码 > 页面锚点，并保留 `explicit` 标记 ——
 * 后台轮只在 `explicit && symbol === 会话绑定标的` 时才敢复用旧会话（见 AiCopilot 的 runInBackground）。
 */
export function resolveTurnSymbol(input: {
  mentioned?: string | null
  text?: string | null
  anchor?: string | null
}): TurnSymbol {
  const mentioned = (input.mentioned || '').trim()
  if (mentioned) return { symbol: mentioned, explicit: true }
  const inText = extractSymbolChips(input.text || '')[0]
  if (inText) return { symbol: inText, explicit: true }
  const anchor = (input.anchor || '').trim()
  return anchor ? { symbol: anchor, explicit: false } : { symbol: '', explicit: false }
}
