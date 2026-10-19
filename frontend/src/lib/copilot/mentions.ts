/**
 * `@` 提及的纯逻辑（便于单测，不碰 DOM）。
 *
 * 触发规则：`@` 位于开头，或前面是空白/中文标点；从 `@` 到光标之间不能有空格。
 * 这条规则决定「邮件地址」「代码里的装饰器」不会误触发。
 */

export interface MentionHit {
  /** `@` 在原文中的下标 */
  start: number
  /** `@` 后面的检索词 */
  query: string
}

const BOUNDARY = /[\s，。；：、？！,.;:?!()（）[\]【】"'“”]/

export function detectMention(text: string, caret: number): MentionHit | null {
  if (!text) return null
  const pos = Math.max(0, Math.min(caret, text.length))
  const head = text.slice(0, pos)
  const at = head.lastIndexOf('@')
  if (at < 0) return null
  if (at > 0 && !BOUNDARY.test(head[at - 1])) return null
  const query = head.slice(at + 1)
  if (/[\s]/.test(query)) return null      // 打了空格就不再是提及
  if (query.length > 12) return null
  return { start: at, query }
}

export function applyMention(text: string, hit: MentionHit, label: string): { text: string; caret: number } {
  const before = text.slice(0, hit.start)
  const after = text.slice(hit.start + 1 + hit.query.length)
  const inserted = `@${label} `
  return { text: before + inserted + after, caret: before.length + inserted.length }
}

export function mentionKey(m: {
  type: string
  value?: string
  symbol?: string
  id?: string
}): string {
  const v = m.value ?? m.symbol ?? m.id ?? ''
  return `${m.type}:${v}`
}

/** 客户端过滤：代码 / 名称 / 拼音首字母（服务端已过滤一次，这里兜住输入抖动）。 */
export function filterMentions<T extends { symbol?: string; name?: string; py?: string; id?: string; label?: string; when?: string[] }>(
  items: T[],
  q: string,
): T[] {
  const kw = q.trim().toLowerCase()
  if (!kw) return items
  return items.filter((it) => {
    const hay = [
      it.symbol, it.name, it.py, it.id, it.label, ...(it.when || []),
    ].filter(Boolean).join(' ').toLowerCase()
    return hay.includes(kw)
  })
}
