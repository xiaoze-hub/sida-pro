/**
 * 回答内价格锚定（2026-10-23，参谋 UX）。
 *
 * 背景：参谋回答里「支撑 32.80 / 阻力 36.5 / 止损位 33.20」全是**死文字** —— 用户看到价位，
 * 却要自己回 K 线图上找。这里只做一件事：把散文里的价位认出来，交给 UI 变成可点的小 chip，
 * 点击回调给上层（K 线画线由另一个模块拥有，本模块**不画图**）。
 *
 * 认不出的宁可不认（漏认 > 误认）：
 *  - 百分比是涨跌幅，不是价位 → kind:'pct'（UI 不渲染成 chip）；
 *  - 6 位股票代码（600111）不是价位 → 丢弃；
 *  - 日期（2026-10-22、2026年）不是价位 → 丢弃；
 *  - 整数（无小数点）只有在紧邻价位关键词或货币符号时才算价位 —— 「3 条依据」不是 3 元。
 *
 * 另一个铁律：`anchorKindLabel` 判不出上下文就返回 `--`，**绝不默认「支撑」**。
 * 把阻力标成支撑，比不标更危险。
 */

export type PriceAnchorKind = 'price' | 'pct' | 'level'

export interface PriceAnchor {
  /** 原文片段（`35.80元` / `35.80`）—— chip 上原样显示，不改写用户看到的文本。 */
  raw: string
  /** 数值（元）。 */
  value: number
  /** 数字前最多 12 字上下文，给 UI 判「支撑/阻力/止损/目标」。 */
  context: string
  kind: PriceAnchorKind
  /** 在原文中的起止位置，供 UI 无损切片。 */
  start: number
  end: number
}

/** 价位词（长词在前，避免「止损位」只认到「止损」以外的残片）。 */
const LEVEL_WORDS = [
  '止损位',
  '止损价',
  '止盈位',
  '止盈价',
  '目标位',
  '目标价',
  '压力位',
  '阻力位',
  '支撑位',
  '成本价',
  '建仓价',
  '加仓位',
  '减仓位',
  '破位',
  '关口',
  '支撑',
  '阻力',
  '压力',
  '止损',
  '止盈',
  '目标',
  '站上',
  '跌破',
  '突破',
  '回踩',
  '现价',
]

/** 词 → 中文标签。长词先判：「止损位」归「止损」，不是「价位」。 */
const CONTEXT_LABELS: { words: string[]; label: string }[] = [
  { words: ['止损'], label: '止损' },
  { words: ['止盈', '目标'], label: '目标' },
  { words: ['阻力', '压力'], label: '阻力' },
  { words: ['支撑'], label: '支撑' },
  { words: ['成本', '建仓', '加仓', '减仓'], label: '价位' },
  { words: ['站上', '跌破', '突破', '回踩', '关口', '破位', '现价'], label: '价位' },
]

/** 上下文只看尾部这几个字：更远的价位词说的是别的数字（「支撑 32.80 阻力 36.5」）。 */
const CONTEXT_LOOKBACK = 6

/** 6 位 A 股/港股代码（600111 / 000001 / 830799），前后不能贴数字。 */
const STOCK_CODE_RE = /(?<!\d)\d{6}(?!\d)/g
/** 日期：2026-10-22 / 2026/10/22 / 2026.10.22 / 2026年10月22日 / 10月22日 / 2026-10。 */
const DATE_RES = [
  /(?<!\d)\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}(?!\d)/g,
  /(?<!\d)\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?/g,
  /(?<!\d)\d{4}\s*-\s*\d{1,2}(?!\d)/g,
  /(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*日/g,
]

/** 空白遮罩：把要排除的片段替换成等长空格，保证后续匹配的 index 仍对齐原文。 */
function mask(text: string, re: RegExp): string {
  return text.replace(re, (m) => ' '.repeat(m.length))
}

function maskAll(text: string): string {
  let out = text
  for (const re of DATE_RES) out = mask(out, re)
  return mask(out, STOCK_CODE_RE)
}

/** 数字本体：可选正负号 + 1~4 位整数 + 可选 0~3 位小数；前后不粘数字/小数点。 */
const NUMBER_RE = /(?<![\d.])([-+]?)(\d{1,4})(?:\.(\d{1,3}))?(?![\d])/g

function tailContext(text: string, index: number, size = 12): string {
  return text
    .slice(Math.max(0, index - size), index)
    .replace(/[，,。；;、：:（）()「」【】\s]+$/g, '')
    .trim()
}

/**
 * 上下文里**离数字最近**的价位词。取最右边出现的那个（同位置取最长）。
 * 「支撑 32.80 阻力 36.5」里，36.5 的上下文含两个词，但最近的是「阻力」。
 */
function nearestLevelWord(context: string): string | null {
  const tail = (context || '').slice(-CONTEXT_LOOKBACK)
  let best: { word: string; at: number } | null = null
  for (const w of LEVEL_WORDS) {
    const at = tail.lastIndexOf(w)
    if (at < 0) continue
    if (!best || at > best.at || (at === best.at && w.length > best.word.length)) {
      best = { word: w, at }
    }
  }
  return best?.word ?? null
}

/**
 * 抽取价位锚点（含 pct，便于调用方统计；UI 只把 price/level 做成 chip）。
 *
 * 验收样例：
 *  - `35.80` / `35.80元` / `站上 35.8` / `支撑 32.80` / `阻力 36.5` / `止损位 33.20` → price|level
 *  - `-1.9%` → pct（不是价位）
 *  - `600111` / `2026-10-22` → 不返回
 */
export function extractPriceAnchors(text: string): PriceAnchor[] {
  const src = text || ''
  if (!src) return []
  const scan = maskAll(src)
  const out: PriceAnchor[] = []

  NUMBER_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = NUMBER_RE.exec(scan)) !== null) {
    const sign = m[1] === '-' ? -1 : 1
    const intPart = m[2]
    const decPart = m[3]
    const start = m.index
    const end = start + m[0].length

    // 紧跟在后面的 `%` → 涨跌幅，不是价位
    const isPct = /^\s*%/.test(scan.slice(end, end + 2))
    const context = tailContext(src, start)
    const levelWord = nearestLevelWord(context)
    const hasCurrency =
      /[¥￥]/.test(context.slice(-2)) || /^\s*元/.test(src.slice(end, end + 2))

    // 整数（无小数点）必须紧邻价位词或货币符号 —— 「3 条」「10 日」「20%」不算价位
    if (decPart == null && !levelWord && !hasCurrency && !isPct) continue

    const value = sign * Number(`${intPart}${decPart != null ? `.${decPart}` : ''}`)
    if (!Number.isFinite(value)) continue
    // 量纲：A 股价位 0.01~10000 元；超出必然是别的数字（成交额/市值/股本）
    if (isPct && Math.abs(value) > 100) continue
    if (!isPct && (Math.abs(value) < 0.01 || Math.abs(value) > 10000)) continue

    // `35.80元` 的「元」并进 chip；负号不进（`-1.9%` 的负号是涨跌幅语义）
    const rawEnd = /^元/.test(src.slice(end, end + 1)) ? end + 1 : end

    out.push({
      raw: src.slice(start, rawEnd),
      value,
      context,
      kind: isPct ? 'pct' : levelWord ? 'level' : 'price',
      start,
      end: rawEnd,
    })
  }

  return out
}

/** 是否值得做成可点 chip：只有 price/level。pct 是涨跌幅，点了没有价位可锚。 */
export function isAnchorClickable(a: Pick<PriceAnchor, 'kind'>): boolean {
  return a.kind === 'price' || a.kind === 'level'
}

/**
 * chip 上的中文标签：支撑 / 阻力 / 止损 / 目标 / 价位；判不出给 `--`（不默认「支撑」）。
 * pct 一律 `--`（它不是价位）。
 */
export function anchorKindLabel(kind: PriceAnchorKind, context = ''): string {
  if (kind === 'pct') return '--'
  const word = nearestLevelWord(context || '')
  if (!word) return kind === 'price' ? '价位' : '--'
  for (const rule of CONTEXT_LABELS) {
    // 「止损位」含「止损」→ 归止损；长词先判所以不会被「价位」类规则截走
    if (rule.words.some((w) => word.includes(w))) return rule.label
  }
  return '--'
}

/**
 * 把一段文本按锚点切成可渲染片段：拼回去与原文逐字相等。
 * UI 只用在「有回调」的分支；无回调时直接渲染原文，不做任何改写。
 */
export type AnchorSegment = { text: string; anchor?: undefined } | { text: string; anchor: PriceAnchor }

export function splitByAnchors(text: string): AnchorSegment[] {
  const src = text || ''
  const anchors = extractPriceAnchors(src).filter(isAnchorClickable)
  if (!anchors.length) return [{ text: src }]
  const segs: AnchorSegment[] = []
  let cursor = 0
  for (const a of anchors) {
    if (a.start < cursor) continue // 重叠（理论不发生）：跳过，保证不重复输出
    if (a.start > cursor) segs.push({ text: src.slice(cursor, a.start) })
    segs.push({ text: src.slice(a.start, a.end), anchor: a })
    cursor = a.end
  }
  if (cursor < src.length) segs.push({ text: src.slice(cursor) })
  return segs
}
