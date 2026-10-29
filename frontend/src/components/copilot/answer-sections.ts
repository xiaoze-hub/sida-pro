/**
 * 回答分节可读性（2026-10-29，回答/证据打磨）。
 *
 * 为什么单独一个模块：`AnswerCard` 只要「排版」，而下面四件事都是**对模型原文的判断**，
 * 混在 JSX 里既没法单测，也最容易在下次改版时被悄悄写成「替模型补内容」：
 *  1. **风险段**：模型写了【风险】才承认有风险。绝不用「跌破/回撤/利空」之类正则从依据里
 *     反推风险 —— 那是编内容，不是排版。`【预案】严格执行风控` 这类合规句也不算风险条目。
 *  2. **数据缺口**：回答自己声明「暂无 / 未取到 / 不可用」的条目从依据里拆出来。这些条目
 *     说明的是**没拿到什么**，和读数混排会让用户把「暂无研报」读成一条利空证据。
 *  3. **来源**：依据尾部 `（腾讯逐笔口径）` 拆成独立来源标记，正文只留读数。只认
 *     「口径 / 数据源 / 来源 / 统计方式 / 计算方式」这几种词，别的一律留在正文（不猜）。
 *  4. **风险文字外溢**：`answer-blocks.parseAnswer` 只认 结论/依据/预案/口径 四个标记，
 *     【风险】段会被并进前一段（通常是依据最后一条或预案）。这里把它截掉，避免同一句
 *     风险既出现在风险块、又粘在依据或预案尾巴上。
 *
 * 全部是纯函数：不读全局、不猜数据，输入就是原文。
 */

/** 风险段标记：`【风险】` / `【风险提示】` / `【风险与失效条件】`。 */
const RISK_MARKER = /【\s*风险[^】]{0,12}】/

/** 段落标记通用形状（与 answer-blocks 的「宽松切段」一致：见到下一个【…】就收尾）。 */
const NEXT_MARKER = /【[^】]{1,14}】/

/**
 * 从原文里取风险条目。没有【风险】段 → `[]`（调用方据此不渲染风险块）。
 *
 * 分条：先按行切，再按中文/英文分号切（模型常把两条风险写在一行里用「；」隔开）。
 */
export function extractRiskLines(raw: string): string[] {
  const text = raw || ''
  const m = RISK_MARKER.exec(text)
  if (!m) return []
  const rest = text.slice(m.index + m[0].length)
  const stop = NEXT_MARKER.exec(rest)
  const block = stop ? rest.slice(0, stop.index) : rest
  return block
    .split('\n')
    .flatMap((line) => line.split(/[;；]/))
    .map((line) => cleanItem(line))
    .filter(Boolean)
}

/** 去掉行首列表符号 / markdown 装饰 / 行尾空标点。 */
function cleanItem(line: string): string {
  return (line || '')
    .replace(/\r/g, '')
    .replace(/\*\*/g, '')
    .replace(/^[\s\-·*•]+/, '')
    .replace(/^\d+[.、)]\s*/, '')
    .replace(/[，,。;；\s]+$/, '')
    .trim()
}

/**
 * 截掉正文里黏上来的风险段（从【风险…】标记一直截到该段末尾）。
 *
 * 只在**同一块**里截：`parseAnswer` 已经按 结论/依据/预案/口径 切过块，所以风险标记后面
 * 剩下的内容本来就属于风险段；这里是「同一句风险不要出现两次」，不是删内容 —— 风险条目
 * 由 `extractRiskLines` 捞出来单独展示。
 */
export function stripRiskTail(text: string): string {
  const src = text || ''
  const m = RISK_MARKER.exec(src)
  if (!m) return src.trim()
  return src.slice(0, m.index).replace(/[\s，,；;、]+$/, '').trim()
}

/** 缺失语义：回答自己说「没拿到」的条目。只认这些词，不认「下跌/流出」这类读数。 */
const GAP_RE = /(暂无|未取到|未返回|未获取|拿不到|不可用|无数据|缺数据|数据缺失|没有取到)/

export function isGapItem(item: string): boolean {
  return GAP_RE.test(item || '')
}

/**
 * 依据拆两块：`data`（真实读数）+ `gaps`（声明的缺失）。
 *
 * 风险段尾巴先截掉（不留重复），截完为空的行直接丢弃 —— 它在风险块里已经展示了。
 * `riskLines` 参数保留在签名里是为了让调用点显式表达「风险与依据共享同一份原文」，
 * 判断本身只依赖原文（不靠风险条目做模糊匹配，避免误伤读数）。
 */
export function partitionEvidence(
  evidence: string[],
  riskLines: string[] = [],
): { data: string[]; gaps: string[] } {
  const data: string[] = []
  const gaps: string[] = []
  void riskLines
  for (const raw of evidence) {
    const body = stripRiskTail(raw)
    if (!body) continue
    if (isGapItem(body)) gaps.push(body)
    else data.push(body)
  }
  return { data, gaps }
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
