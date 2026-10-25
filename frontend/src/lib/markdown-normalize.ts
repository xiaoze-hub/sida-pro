/**
 * Markdown 表格归一化（2026-10-25）。
 *
 * **为什么需要**：参谋回答里经常出现这种形态
 * ```
 * 也就是说，从60日、120日维度看上证是上涨趋势：
 * | 外部因素 | 数据 | 方向 |
 * | :--- | :--- | :--- |
 * ```
 * 段落与表格之间**没有空行** —— GFM 规定表格必须另起一个块，紧贴上一段时会被并进段落，
 * 于是整张表退化成纯文本管道符（用户在截图里看到的就是这样）。
 *
 * 修的是「渲染前归一化」，不是「教模型加空行」：
 * 模型输出格式天然不稳定，靠提示词保证等于把 UI 质量押在概率上。
 */

/** 表格对齐行：|---|:---:|---:| 这种 */
const isDelimiterRow = (line: string): boolean =>
  /^\s*\|?[\s:-]*-[\s|:-]*\|?\s*$/.test(line) && line.includes('-')

/** 表格内容行：以 | 开头，或含至少一个 | 分段 */
const isTableRow = (line: string): boolean => /^\s*\|/.test(line) || /^\s*[^|\s][^|]*\|[^|]*$/.test(line)

/**
 * 在紧贴段落的表格前补空行；顺带把「表头 + 分隔行」之间被塞进来的空行去掉。
 * 幂等：对已经正确的文本再跑一次不会有变化。
 */
export function normalizeMarkdownTables(md: string): string {
  if (!md || !md.includes('|')) return md
  const lines = md.split('\n')
  const out: string[] = []

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    const prev = out.length ? out[out.length - 1] : ''
    const next = lines[i + 1] ?? ''

    // ① 当前行是表格内容行、上一行是有内容的普通文本 → 插空行
    const startsTable =
      isTableRow(line) &&
      (!isTableRow(prev) || isDelimiterRow(line)) &&
      prev.trim() !== '' &&
      !isTableRow(prev) &&
      // 标题后面接表格也补空行（GFM 更稳；多一个空行对渲染无副作用）
      !prev.trim().startsWith('```')

    if (startsTable && isDelimiterRow(next)) {
      // 只有「表头行 + 分隔行」才认定为表格；避免把 `a | b` 这种普通文本误判
      out.push('')
    }
    out.push(line)
  }

  return out.join('\n')
}

export default normalizeMarkdownTables
