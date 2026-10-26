/**
 * 参谋回答 → 结构化块（2026-10-19, 数智参谋排版 P1；同日晚按**真实模型输出**重写）。
 *
 * 为什么不靠模型返回 JSON：流式回答必须逐字可读，JSON 在流中不可渲染。
 * 契约来源是 `src/web/api/chat.py` 的「参谋回答模板」，但真实输出比模板花得多：
 *   - 标题带 markdown：`## 【结论】`、`### ① 实时行情`、`---` 分隔线；
 *   - 依据常写成 markdown 表格（`| 维度 | 关键读数 | 口径 |`）；
 *   - 段落名有变体：`【口径汇总】`、`【口径说明】`、`【实战作战预案】`；
 *   - 加粗 `**8.92 元**` 满天飞（卡片里不能把星号露给用户）。
 * 所以这里是「宽松切段 + 去 markdown」，解析不出来就整段回退原文 —— 绝不吃掉回答。
 */

export type SectionKey = '结论' | '依据' | '预案' | '口径'

export interface ParsedAnswer {
  /** 是否成功切出段落（false 时前端回退 SafeMarkdown） */
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

const MARKER_RE = /【\s*(结论|依据|预案|口径)[^】]*】/g
const DISCLAIMER_RE = /以上(?:为|分析|内容)[^。\n]*不构成投资建议[。]?/g

/** 去掉 markdown 装饰，保留可读文本。 */
export function stripMd(input: string): string {
  return (input || '')
    .replace(/\r/g, '')
    .replace(/^\s*#{1,6}\s*/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*[-*_]{3,}\s*$/gm, '')
    .replace(/\*\*(.+?)\*\*/g, '$1')
    .replace(/__(.+?)__/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*/g, '')
    .trim()
}

function canonical(key: string): SectionKey {
  if (key.startsWith('依据')) return '依据'
  if (key.startsWith('预案')) return '预案'
  if (key.startsWith('口径')) return '口径'
  return '结论'
}

/** markdown 表格行 → 「维度：读数（口径）」。 */
function tableRows(block: string): string[] {
  const rows: string[] = []
  for (const line of block.split('\n')) {
    const t = line.trim()
    if (!t.startsWith('|')) continue
    if (/^\|[\s:|-]+\|$/.test(t)) {
      // 分隔行 → 上一行是表头，别当数据
      rows.pop()
      continue
    }
    const cells = t
      .split('|')
      .map((c) => stripMd(c).trim())
      .filter((c, i, arr) => !(c === '' && (i === 0 || i === arr.length - 1)))
      .filter((c) => c.length > 0)
    if (!cells.length) continue
    const [head, ...rest] = cells
    rows.push(rest.length ? `${head}：${rest[0]}${rest[1] ? `（${rest.slice(1).join('，')}）` : ''}` : head)
  }
  return rows
}

/** 依据段拆条目：表格优先；否则按行首 `-`/`·`/`*`/`1.`/`①` 切，续行并入上一条。 */
export function splitEvidence(block: string): string[] {
  const table = tableRows(block)
  if (table.length) return table

  const items: string[] = []
  let current = ''
  for (const rawLine of block.split('\n')) {
    const line = stripMd(rawLine)
    if (!line) continue
    const isItem = /^([-·*•]|\d+[.、)]|[①②③④⑤⑥⑦⑧⑨⑩])\s*/.test(line)
    if (isItem) {
      if (current) items.push(current)
      current = line.replace(/^([-·*•]|\d+[.、)])\s*/, '')
    } else if (current) {
      current += ` ${line}`
    } else {
      current = line
    }
  }
  if (current) items.push(current)
  return items.map((s) => s.trim()).filter(Boolean)
}

/** 置信度必须来自原文的 `x/10`（允许 `**4/10**`），拿不到就给 null —— 不许猜。 */
export function extractConfidence(text: string): number | null {
  const m = (text || '').replace(/\*/g, '').match(/置信度[^\d]{0,8}(\d+(?:\.\d+)?)\s*\/\s*10/)
  if (!m) return null
  const v = Number(m[1])
  return Number.isFinite(v) ? v : null
}

/** 结论段拆段落：首段做裁决，其余段落并入依据（不丢内容）。 */
function splitVerdict(block: string): { verdict: string; extra: string[] } {
  const paras = stripMd(block)
    .split(/\n\s*\n/)
    .map((p) => p.replace(/\s+/g, ' ').trim())
    .filter(Boolean)
  if (!paras.length) return { verdict: '', extra: [] }
  // 置信度单独做徽标，正文里去掉，避免同一个数字出现两次
  const verdict = paras[0]
    .replace(/[，,、]?\s*置信度\s*[:：]?\s*\d+(?:\.\d+)?\s*\/\s*10\s*[（(]?/, (m) => (m.includes('（') || m.includes('(') ? '（' : ''))
    .replace(/\s{2,}/g, ' ')
    .trim()
  return { verdict, extra: paras.slice(1) }
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

  const matches = [...text.matchAll(MARKER_RE)]
  if (!matches.length) return empty

  const blocks: Partial<Record<SectionKey, string>> = {}
  matches.forEach((m, i) => {
    const start = (m.index ?? 0) + m[0].length
    const end = i + 1 < matches.length ? (matches[i + 1].index ?? text.length) : text.length
    blocks[canonical(m[1])] = text.slice(start, end)
  })

  const disclaimerMatch = text.match(DISCLAIMER_RE)
  const disclaimer = disclaimerMatch ? disclaimerMatch[0].trim() : ''

  const { verdict, extra } = splitVerdict(blocks['结论'] || '')
  const confidence = extractConfidence(blocks['结论'] || text)
  const evidence = [...splitEvidence(blocks['依据'] || ''), ...extra]
  const plan = stripMd((blocks['预案'] || '').replace(DISCLAIMER_RE, '')).replace(/\n{3,}/g, '\n\n')
  const caliber = stripMd(blocks['口径'] || '')
    .split('\n')
    .map((l) => l.replace(/^[-·]\s*/, '').trim())
    .filter(Boolean)
    .join(' · ')

  if (!verdict && !evidence.length && !plan && !caliber) return empty

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
