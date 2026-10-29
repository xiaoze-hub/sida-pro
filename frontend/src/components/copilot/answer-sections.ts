/**
 * 回答分节可读性（2026-10-29，回答/证据打磨；同日第二轮按审查修复）。
 *
 * 为什么单独一个模块：`AnswerCard` 只要「排版」，而下面几件事都是**对模型原文的判断**，
 * 混在 JSX 里既没法单测，也最容易在下次改版时被悄悄写成「替模型补内容」：
 *  1. **风险段**：模型写了【风险】才承认有风险。绝不用「跌破/回撤/利空」之类正则从依据里
 *     反推风险 —— 那是编内容，不是排版。`【预案】严格执行风控` 这类合规句也不算风险条目。
 *     一份回答里可能有多段风险（【风险】…【风险提示】…），**每一段都要捞出来**。
 *  2. **数据缺口**：回答自己声明「暂无 / 未取到 / 不可用」的条目从依据里拆出来。这些条目
 *     说明的是**没拿到什么**，和读数混排会让用户把「暂无研报」读成一条利空证据。
 *     风险段里混进来的缺失条目同样归缺口，不能冒充失效条件。
 *  3. **来源**：依据尾部 `（腾讯逐笔口径）` 拆成独立来源标记，正文只留读数。只认
 *     「口径 / 数据源 / 来源 / 统计方式 / 计算方式」这几种词，别的一律留在正文（不猜）。
 *  4. **风险文字外溢**：`answer-blocks.parseAnswer` 只认 结论/依据/预案/口径 四个标记，
 *     【风险】段会被并进前一段（通常是依据最后一条或预案）。这里只截掉**风险段本身**，
 *     段后面的其它分节（如【预案】）一个字都不许跟着删。
 *
 * 分节边界只认**已知分节词**（结论/依据/预案/口径/风险/数据缺口…）：
 *   - `【002594】` 这类股票代码、`【依据央行数据】` 之外的普通方括号**不是**分节标记；
 *   - markdown 标题（`### 失效条件` / `### 数据缺口`）是结构不是内容：标题本身不当条目，
 *     但标题要是另一个分节名（如「数据缺口」）就算边界，它底下的条目归对应分节。
 *
 * 铁律：**任何内容都不许丢**。判断不了就退回原文，绝不因为「排版好看」吞掉模型写的字。
 *
 * 全部是纯函数：不读全局、不猜数据，输入就是原文。
 */

/** 风险段标记：`【风险】` / `【风险提示】` / `【风险与失效条件】`。 */
const RISK_MARKER = /【\s*风险[^】]{0,12}】/

/** 分节词：只有这些词算「下一段」边界（`【002594】` 是代码，不是分节）。 */
const SECTION_WORDS =
  /^(结论|依据|证据|预案|口径|风险|数据缺口|缺口|缺失数据|数据缺失|免责|置信度|来源|统计|说明|备注|局限|下一步|追问)/

/** 标题式分节：不含「风险」—— 风险段内部的小标题（`### 失效条件`）不该把风险段截断。 */
const HEADING_SECTION_WORDS =
  /^(结论|依据|证据|预案|口径|数据缺口|缺口|缺失数据|数据缺失|免责|置信度|来源|统计|说明|备注|局限|下一步|追问)/

/** 缺口式分节（`【数据缺口】` / `### 数据缺口`）。 */
const GAP_SECTION_WORDS = /^(数据缺口|缺口|缺失数据|数据缺失|未取到)/

const BRACKET_RE = /【\s*([^】]{1,20})】/
const HEADING_RE = /(^|\n)([ \t]*#{1,6}[ \t]*)([^\n]*)/

/** 该行是不是 markdown 标题（结构，不是内容）。 */
function isHeadingLine(line: string): boolean {
  return /^[ \t]*#{1,6}[ \t]*\S/.test(line)
}

/** 该行是不是分隔线。 */
function isHrLine(line: string): boolean {
  return /^[ \t]*[-*_]{3,}[ \t]*$/.test(line)
}

/** 从 `from` 起，第一个「括号式分节标记」的位置；没有就是文末。 */
function bracketBoundary(text: string, from: number, words = SECTION_WORDS): number {
  const re = new RegExp(BRACKET_RE.source, 'g')
  re.lastIndex = from
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    if (words.test(m[1].trim())) return m.index
  }
  return text.length
}

/** 从 `from` 起，第一个「标题式分节」的行首位置；没有就是文末。 */
function headingBoundary(text: string, from: number, words = HEADING_SECTION_WORDS): number {
  const re = new RegExp(HEADING_RE.source, 'g')
  re.lastIndex = from
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    const head = m[3].trim()
    if (head && words.test(head)) return m.index + m[1].length
    if (re.lastIndex <= m.index) re.lastIndex = m.index + 1
  }
  return text.length
}

/** 下一段从这里开始（括号式 / 标题式谁先到算谁）。 */
function nextBoundary(text: string, from: number): number {
  return Math.min(bracketBoundary(text, from), headingBoundary(text, from))
}

interface RiskSpan {
  /** 风险标记起点（截断时连标记一起删） */
  markerStart: number
  /** 风险正文起点 */
  bodyStart: number
  /** 风险段终点（下一个已知分节 / 文末） */
  end: number
}

/** 找出**每一段**风险的位置。多段风险各算各的，互不吞并。 */
function riskSpans(text: string): RiskSpan[] {
  const spans: RiskSpan[] = []
  const re = new RegExp(RISK_MARKER.source, 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    const bodyStart = m.index + m[0].length
    const end = Math.max(bodyStart, nextBoundary(text, bodyStart))
    spans.push({ markerStart: m.index, bodyStart, end })
  }
  return spans
}

/** 风险段正文 → 条目：跳过标题 / 分隔线，按行与「；」切。 */
function riskItems(body: string): string[] {
  const items: string[] = []
  for (const rawLine of body.split('\n')) {
    const line = rawLine.replace(/\r/g, '')
    if (isHeadingLine(line) || isHrLine(line)) continue
    for (const piece of line.split(/[;；]/)) {
      const item = cleanItem(piece)
      if (item) items.push(item)
    }
  }
  return items
}

/**
 * 风险条目 + 风险段里声明的缺失。
 *
 * 缺失条目（「研报数据未取到」）就算被模型写进【风险】段也归缺口 —— 它说明的是没拿到什么，
 * 不是失效条件；两边都展示会让同一个「暂无」既像风险又像缺口。
 */
export function scanRisk(raw: string): { risks: string[]; gaps: string[] } {
  const text = raw || ''
  const risks: string[] = []
  const gaps: string[] = []
  for (const span of riskSpans(text)) {
    for (const item of riskItems(text.slice(span.bodyStart, span.end))) {
      if (isGapItem(item)) gaps.push(item)
      else risks.push(item)
    }
  }
  return { risks, gaps }
}

/** 从原文里取风险条目（多段合并）。没有【风险】段 → `[]`（调用方据此不渲染风险块）。 */
export function extractRiskLines(raw: string): string[] {
  return scanRisk(raw).risks
}

/** 去掉行首的**已知分节标记**（`【数据缺口】`）；`【002594】` 是内容，一个字都不许删。 */
function stripLeadingSectionMarker(line: string): string {
  const m = /^【\s*([^】]{1,20})】\s*/.exec(line || '')
  if (!m) return line
  return SECTION_WORDS.test(m[1].trim()) ? line.slice(m[0].length) : line
}

/** 去掉行首列表符号 / markdown 装饰 / 分节标记 / 行尾空标点。 */
function cleanItem(line: string): string {
  let s = (line || '').replace(/\r/g, '').replace(/\*\*/g, '')
  s = s.replace(/^[\s\-·*•]+/, '')
  s = stripLeadingSectionMarker(s)
  s = s.replace(/^[-·*•]\s*/, '')
  s = s.replace(/^\d+[.、)]\s*/, '')
  s = s.replace(/[，,。;；\s]+$/, '')
  return s.trim()
}

/**
 * 截掉正文里黏上来的风险段（**只删风险段本身**）。
 *
 * `parseAnswer` 已经按 结论/依据/预案/口径 切过块，所以【风险】之后跟着的要么是风险正文，
 * 要么是下一个分节（`【预案】` / `### 数据缺口`）。前者由 `extractRiskLines` 单独展示，
 * 后者必须原样留下 —— 早先「从标记截到块尾」的实现会把整段风险后面的内容一起删掉。
 * 有多段风险时逐段处理，剩下的内容按原文顺序拼回去。
 */
export function stripRiskTail(text: string): string {
  const src = text || ''
  const spans = riskSpans(src)
  if (!spans.length) return src.trim()

  const parts: string[] = []
  let cursor = 0
  for (const span of spans) {
    parts.push(src.slice(cursor, span.markerStart))
    cursor = Math.max(cursor, span.end)
  }
  parts.push(src.slice(cursor))

  return parts
    .map((p) => p.replace(/^[\s，,；;、]+/, '').replace(/[\s，,；;、]+$/, '').trim())
    .filter(Boolean)
    .join('\n')
}

/** 缺失语义：回答自己说「没拿到」的条目。只认这些词，不认「下跌/流出」这类读数。 */
const GAP_RE = /(暂无|未取到|未返回|未获取|拿不到|不可用|无数据|缺数据|数据缺失|没有取到)/

export function isGapItem(item: string): boolean {
  return GAP_RE.test(item || '')
}

/**
 * 按已知分节标记把一段正文切开（`底仓成本 40.16 元 【数据缺口】 - 研报数据未取到`
 * → 读数一条 + 缺口一条）。
 *
 * `splitEvidence` 会把续行并进上一条，所以一个「依据条目」里可能塞着下一个分节的标记与内容；
 * 不切开会把真实读数和「未取到」并成同一条，读数就被算进缺口里了。
 */
function splitSectionPieces(text: string): string[] {
  const src = text || ''
  const cuts: number[] = []
  const re = new RegExp(BRACKET_RE.source, 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(src))) {
    if (SECTION_WORDS.test(m[1].trim())) cuts.push(m.index)
  }
  if (!cuts.length) return [src]
  const pieces: string[] = []
  let cursor = 0
  for (const cut of cuts) {
    pieces.push(src.slice(cursor, cut))
    cursor = cut
  }
  pieces.push(src.slice(cursor))
  return pieces.filter((p) => p.trim())
}

/** 归一化：只比「字」，不比空白 / 列表符号 / 行尾标点。 */
function normKey(s: string): string {
  return (s || '')
    .replace(/\*\*/g, '')
    .replace(/\s+/g, '')
    .replace(/^[-·*•]+/, '')
    .replace(/[，,。;；、]+$/g, '')
    .trim()
}

/** 一段多行正文 → 行数组（去空行）。口径 / 预案这类分节的行集合，用来判断「是否已经展示过」。 */
export function splitLines(text: string): string[] {
  return (text || '')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
}

/**
 * 依据拆两块：`data`（真实读数）+ `gaps`（声明的缺失）。
 *
 * 风险段尾巴先截掉（留在风险块里展示，不留重复），截完剩的分节片段再逐条归类。
 * `riskLines` 参数保留在签名里是为了让调用点显式表达「风险与依据共享同一份原文」，
 * 判断本身只依赖原文（不靠风险条目做模糊匹配，避免误伤读数）。
 *
 * `shownElsewhere`：已经单独成节展示过的行（缺口段 / 预案 / 口径）。模型的【风险】【预案】
 * 标记常常单独占一行，`splitEvidence` 会把标记后的条目切成**新的依据条目** —— 那些条目
 * 已经在风险 / 预案块里出现过，再在依据里列一遍就是同一句话出现两次（还会把依据条数算多）。
 * 判断只认「这一条字面上就在别的分节里」，不做语义猜测。
 */
export function partitionEvidence(
  evidence: string[],
  riskLines: string[] = [],
  shownElsewhere: string[] = [],
): { data: string[]; gaps: string[] } {
  const data: string[] = []
  const gaps: string[] = []
  const shown = [...riskLines, ...shownElsewhere].map(normKey).filter(Boolean)
  const shownSet = new Set(shown)
  /** 连续 2~3 行拼起来的结果：`splitEvidence` 会把续行并成一条，这里对得上才丢。 */
  const joinedSet = new Set<string>()
  for (let i = 0; i < shown.length; i += 1) {
    joinedSet.add(`${shown[i]}${shown[i + 1] ?? ''}`)
    joinedSet.add(`${shown[i]}${shown[i + 1] ?? ''}${shown[i + 2] ?? ''}`)
  }
  /**
   * 只认**整条相同**（含连续多行拼起来相同）：读数恰好被预案里的一句话提到，
   * 不能因为「字面包含」就从依据里删掉 —— 那才是丢证据。
   *
   * 另有一种「只剩装饰」的相同：markdown 标题在 `parseAnswer` 里被去掉 `#` 后，
   * 会以 `数据缺口` 这样的裸标题词粘在条目尾巴上（`【002594】…失效 数据缺口`）。
   * 这种尾巴只是一段小标题的残渣，允许它跟着整条一起去重。
   */
  const SECTION_TAIL = /^(数据缺口|缺口|风险提示|风险|预案|口径|依据|结论|置信度|来源|说明|备注|局限)+$/
  const alreadyShown = (piece: string): boolean => {
    const key = normKey(piece)
    if (!key) return false
    if (shownSet.has(key) || joinedSet.has(key)) return true
    for (const shownKey of shown) {
      if (shownKey.length < 6 || !key.startsWith(shownKey)) continue
      const rest = key.slice(shownKey.length)
      if (rest && rest.length <= 8 && SECTION_TAIL.test(rest)) return true
    }
    return false
  }
  for (const raw of evidence) {
    const residual = stripRiskTail(raw)
    for (const piece of splitSectionPieces(residual)) {
      const body = cleanItem(piece)
      if (!body) continue
      if (alreadyShown(body)) continue
      if (isGapItem(body)) gaps.push(body)
      else data.push(body)
    }
  }
  return { data, gaps }
}

/**
 * 原文里「数据缺口」分节的条目（`【数据缺口】` 括号式，或 `### 数据缺口` 标题式）。
 *
 * 为什么要在原文上单独扫一遍：模型常常把缺口写成一个小标题，这段内容在 `parseAnswer`
 * 里会被并进依据尾部（甚至并进风险段）。只靠依据条目归类，会让缺口条目被风险段吞掉。
 */
export function extractGapLines(raw: string): string[] {
  const text = raw || ''
  const starts: number[] = []

  const bracket = new RegExp(BRACKET_RE.source, 'g')
  let bm: RegExpExecArray | null
  while ((bm = bracket.exec(text))) {
    if (GAP_SECTION_WORDS.test(bm[1].trim())) starts.push(bm.index + bm[0].length)
  }

  const heading = new RegExp(HEADING_RE.source, 'g')
  let hm: RegExpExecArray | null
  while ((hm = heading.exec(text))) {
    if (GAP_SECTION_WORDS.test(hm[3].trim())) starts.push(hm.index + hm[0].length)
  }

  const out: string[] = []
  for (const start of starts) {
    const body = text.slice(start, Math.max(start, nextBoundary(text, start)))
    for (const rawLine of body.split('\n')) {
      const line = rawLine.replace(/\r/g, '')
      if (isHeadingLine(line) || isHrLine(line)) continue
      for (const piece of line.split(/[;；]/)) {
        const item = cleanItem(piece)
        if (item) out.push(item)
      }
    }
  }
  return out
}

/** 尾部括号里的来源词：只有这几种才算「来源/口径」，别的一律留在正文。 */
const SOURCE_WORD = /(口径|数据源|来源|统计方式|计算方式)/
const TRAILING_PAREN = /^(.*?)\s*[（(]([^（）()]{1,24})[）)]\s*$/

/**
 * `逐笔主力净流出 552 万（腾讯逐笔口径）` → `{ body: '逐笔主力净流出 552 万', source: '腾讯逐笔口径' }`。
 * 不是来源括号（`（近 20 日成交密集区）`）→ source 为 null，正文保持**原文一字不改**。
 */
export function splitEvidenceSource(item: string): { body: string; source: string | null } {
  const text = (item || '').trim()
  const m = TRAILING_PAREN.exec(text)
  if (!m) return { body: text, source: null }
  const body = m[1].trim()
  const source = m[2].trim()
  if (!body || !source || !SOURCE_WORD.test(source)) return { body: text, source: null }
  return { body, source }
}

/** 口径串 → 条目（`腾讯逐笔 + 自算CYQ` 这类整行，按 `·` / `+` / 顿号拆）。 */
export function splitCaliber(caliber: string): string[] {
  return (caliber || '')
    .replace(/\r/g, '')
    .split(/[·、]|(?:\s+\+\s+)|\s*\|\s*/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/**
 * 去掉正文里重复出现的免责句。
 *
 * 为什么需要：后端模板把「末尾固定一行免责」写在【口径】之后，`parseAnswer` 按标记切段时
 * 这句会落进口径块 —— 卡片底部已经有一行免责，口径里再出现一次就变成「口径 = 腾讯逐笔 +
 * 以上仅供参考，不构成投资建议」，读起来像是免责声明也是数据口径的一部分。
 * 只删除**结尾标点一致的整句**，不做模糊匹配。
 */
export function stripDisclaimer(text: string, disclaimer: string): string {
  const src = (text || '').trim()
  const tail = (disclaimer || '').trim()
  if (!src || !tail) return src
  return src
    .split(tail)
    .join(' ')
    .replace(/[\s·、，,;；]+$/, '')
    .replace(/\s{2,}/g, ' ')
    .trim()
}
