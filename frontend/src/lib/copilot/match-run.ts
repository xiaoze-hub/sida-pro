import type { CopilotRunRow } from '@/lib/copilot/api'

/**
 * 把一条助手消息对到它那一轮 copilot run（2026-10-25）。
 *
 * **为什么按提问文本匹配**：消息表里没有 run_id，但一轮问答 = 一个提问，
 * `runs` 带着 `question`。用「该助手消息前面最近的一条用户消息」的原文去比，
 * 比按下标对齐稳（历史里有系统消息、失败重试、并发插入，下标会错位）。
 *
 * **匹配不到就返回 null** —— 宁可不显示步骤条，也不能张冠李戴把别人的工具链挂上去。
 * 那种错误比不显示严重得多：用户会以为「这条结论真的调了这些工具」。
 */
export function matchRunForMessage(
  msgs: { role: string; content: string }[],
  runs: CopilotRunRow[],
  index: number,
): CopilotRunRow | null {
  const m = msgs[index]
  if (!m || m.role !== 'assistant') return null
  if (!Array.isArray(runs) || runs.length === 0) return null

  let question = ''
  for (let k = index - 1; k >= 0; k -= 1) {
    if (msgs[k].role === 'user') {
      question = (msgs[k].content || '').trim()
      break
    }
  }
  if (!question) return null

  const norm = (t: string) =>
    (t || '')
      .replace(/\s+/g, ' ')
      // 输入框支持 @ 提及，实际发出去的问题可能带 "@某某 " 前缀；后端存的 question 不一定带
      .replace(/^(@\S+\s+)+/, '')
      .trim()

  const q = norm(question)
  if (!q) return null

  // ① 精确（归一化后）→ ② 前缀 24 字（防止一端被截断/多了标点）
  const exact = runs.find((r) => norm(r.question || '') === q)
  if (exact) return exact

  const head = q.slice(0, 24)
  if (head.length < 8) return null
  const loose = runs.find((r) => {
    const rq = norm(r.question || '')
    return rq.startsWith(head) || q.startsWith(rq.slice(0, 24))
  })
  return loose ?? null
}
