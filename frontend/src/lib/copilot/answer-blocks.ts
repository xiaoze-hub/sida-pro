/**
 * 参谋回答 → 结构化块（2026-10-19, 数智参谋排版 P1）。
 *
 * 为什么不靠模型返回 JSON：流式回答必须逐字可读，JSON 在流中不可渲染；
 * 而 `SYSTEM_PROMPT` 已经强制模型按【结论】【依据】【预案】【口径】四段输出
 * （见 `src/web/api/chat.py` 的「参谋回答模板」）。所以前端按这四个标记切段，
 * 把散文变成卡片 —— 契约在后端已有，前端只是换一种渲染方式。
 *
 * 解析失败（模型没按模板写 / 用户问的是闲聊）必须原样回退 markdown，
 * 绝不吃掉用户的回答。
 */

export type SectionKey = '结论' | '依据' | '预案' | '口径'

const MARKER_RE = /【(结论|依据|预案|口径)】/g
const DISCLAIMER_RE = /以上(?:为|分析)[^。\n]*不构成投资建议[。]?/g

export interface ParsedAnswer {
  /** 是否成功切出四段（false 时前端回退 SafeMarkdown） */
  structured: boolean
  verdict: string
  /** 从「置信度 x/10」里解析出的分值；没有则 null（不编造） */
  confidence: number | null
  evidence: string[]
  plan: string
  caliber: string
  disclaimer: string
  raw: string
}

function clean(text: string): string {
  return text.replace(/^[\s:：]+/, '').replace(/\s+$/, '')
}

/** 依据段拆条目：按行首 `-`/`·`/`*`/`1.` 切，保留条目正文。 */
export function splitEvidence(block: string): string[] {
  const lines = block.split('\n')
  const items: string[] = []
  let current = ''
  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const isItem = /^([-·*•]|\d+[.、)])\s*/.test(trimmed)
    if (isItem) {
      if (current) items.push(current)
      current = trimmed.replace(/^([-·*•]|\d+[.、)])\s*/, '')
    } else if (current) {
      current += ` ${trimmed}`
    } else {
      current = trimmed
    }
  }
  if (current) items.push(current)
  return items.filter((s) => s.length > 0)
}

/** 置信度必须来自原文的 `x/10`，拿不到就给 null —— 不允许前端猜一个数出来。 */
export function extractConfidence(text: string): number | null {
  const m = text.match(/置信度[^\d]{0,6}(\d+(?:\.\d+)?)\s*\/\s*10/)
  if (!m) return null
  const v = Number(m[1])
  return Number.isFinite(v) ? v : null
}

export function parseAnswer(raw: string): ParsedAnswer {
  const text = (raw || '').trim()
  const empty: ParsedAnswer = {
    structured: false,
    verdict: '',
    confidence: null,
    evidence: [],
    plan: '',
    caliber: '',
    disclaimer: '',
    raw: text,
  }
  if (!text) return empty

  const blocks: Partial<Record<SectionKey, string>> = {}
  const matches = [...text.matchAll(MARKER_RE)]
  if (!matches.length) return empty

  matches.forEach((m, i) => {
    const key = m[1] as SectionKey
    const start = (m.index ?? 0) + m[0].length
    const end = i + 1 < matches.length ? (matches[i + 1].index ?? text.length) : text.length
    const chunk = text.slice(start, end)
    // 同段重复出现时以最后一次为准（模型偶尔会重复标题）
    blocks[key] = chunk
  })

  const disclaimerMatch = text.match(DISCLAIMER_RE)
  const disclaimer = disclaimerMatch ? disclaimerMatch[0] : ''

  const verdictBlock = clean(blocks['结论'] || '')
  const evidenceBlock = blocks['依据'] || ''
  const plan = clean((blocks['预案'] || '').replace(DISCLAIMER_RE, ''))
  const caliber = clean(blocks['口径'] || '')

  // 结论段里可能自带「置信度 x/10」，从原文提取，不从结论里再猜
  const confidence = extractConfidence(`${verdictBlock}\n${text}`)

  const evidence = splitEvidence(evidenceBlock.replace(DISCLAIMER_RE, ''))

  // 四段全空 = 没按模板写，回退原文
  if (!verdictBlock && !evidence.length && !plan && !caliber) return empty

  // 结论里剔除「置信度…」尾巴，避免和徽标重复
  const verdict = verdictBlock
    .replace(/[，,、]?\s*置信度[^\d]{0,6}\d+(?:\.\d+)?\s*\/\s*10/, '')
    .replace(/\s+/g, ' ')
    .trim()

  return {
    structured: true,
    verdict,
    confidence,
    evidence,
    plan,
    caliber,
    disclaimer: disclaimer || '以上为客观数据推演，不构成投资建议。',
    raw: text,
  }
}
