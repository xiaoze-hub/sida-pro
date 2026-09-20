/**
 * 截断的文字要能看全（2026-09-20）。
 *
 * 由来：布局体检新加"截断看不全"判据后，在生产上量出 `/history` **25 处**报告标题被截到
 * 256px 且**没有任何途径看全**（无 `title`/`aria-label`）—— 用户永远不知道那份报告叫什么。
 * 首页也有 2 处结论文字同样被截。
 *
 * 规则：**`truncate`（省略号）本身是设计选择，但"看不全"就是内容丢失** —— 内容型元素
 * 截断时必须带 `title`（悬停可见）或 `aria-label`。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const history = readFileSync(resolve(__dirname, '../../src/pages/History.tsx'), 'utf-8')
const dashboard = readFileSync(resolve(__dirname, '../../src/pages/Dashboard.tsx'), 'utf-8')

describe('截断必须能看全', () => {
  it('/history 报告标题截断 → 带 title（悬停看全名）', () => {
    // 该 span 同时有 truncate 与 title={r.title ...}
    expect(history).toMatch(/truncate[\s\S]{0,120}title=\{r\.title \|\| '分析报告'\}/)
  })

  it('首页结论/理由类说明截断 → 带 title', () => {
    const truncated = dashboard.match(/truncate text-\[11px\] text-muted-foreground/g) || []
    const withTitle = dashboard.match(/truncate text-\[11px\] text-muted-foreground" title=/g) || []
    expect(truncated.length).toBeGreaterThan(0)
    expect(withTitle.length).toBe(truncated.length)
  })

  it('不该用 title 的地方别乱加（不是给所有 truncate 都套 title 就完事）', () => {
    // 静态文案截断无需 title（内容固定、截了也无所谓）—— 这条只是提醒：修的是"内容丢失"，
    // 不是"见到 truncate 就加 title"。真判据在生产探针里（按页面实测"看不全"的数量）。
    expect(history).not.toMatch(/title="分析报告"/)
  })
})
