// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import SafeMarkdown from '@/components/SafeMarkdown'
import { normalizeMarkdownTables } from '@/lib/markdown-normalize'

afterEach(() => cleanup())

describe('normalizeMarkdownTables', () => {
  it('紧贴段落的表格前补空行（否则 GFM 会把表格并进段落 → 退化成文本管道符）', () => {
    const src = '从60日维度看是上涨趋势：\n| 因素 | 数据 |\n| --- | --- |\n| 美债 | 4.99% |'
    const out = normalizeMarkdownTables(src)
    expect(out).toContain('趋势：\n\n| 因素 | 数据 |')
  })

  it('已经有空行的不动（幂等）', () => {
    const src = '结论：\n\n| 因素 | 数据 |\n| --- | --- |\n| 美债 | 4.99% |'
    expect(normalizeMarkdownTables(src)).toBe(src)
    // 再跑一次还是原样
    expect(normalizeMarkdownTables(normalizeMarkdownTables(src))).toBe(src)
  })

  it('表格后面紧跟正文不受影响', () => {
    const src = '| a | b |\n| --- | --- |\n| 1 | 2 |\n所以结论是X。'
    const out = normalizeMarkdownTables(src)
    expect(out).toContain('| 1 | 2 |\n所以结论是X。')
  })

  it('不含管道符的文本原样返回', () => {
    const src = '没有表格的一段话。'
    expect(normalizeMarkdownTables(src)).toBe(src)
  })

  it('代码块里的管道符不误判', () => {
    const src = '示例：\n```bash\nps aux | grep python\n```'
    expect(normalizeMarkdownTables(src)).toBe(src)
  })

  it('标题下面的表格也要补空行', () => {
    const src = '## 外部因素\n| 因素 | 数据 |\n| --- | --- |\n| 油价 | 116 |'
    const out = normalizeMarkdownTables(src)
    expect(out).toContain('## 外部因素\n\n| 因素 | 数据 |')
  })
})

describe('SafeMarkdown 渲染', () => {
  it('表格真的渲染成 table，而不是纯文本管道符', () => {
    render(
      <SafeMarkdown>
        {'从60日维度看是上涨趋势：\n| 因素 | 数据 |\n| --- | --- |\n| 美债 | 4.99% |'}
      </SafeMarkdown>,
    )
    const table = document.querySelector('table')
    expect(table).toBeTruthy()
    expect(screen.getByText('因素')).toBeTruthy()
    expect(screen.getByText('4.99%')).toBeTruthy()
    // 关键：不应把整行原样当文本渲染
    expect(document.body.textContent).not.toContain('| --- |')
  })

  it('数值单元格带 tabular-nums（列对齐）', () => {
    render(<SafeMarkdown>{'| a |\n| --- |\n| 1 |'}</SafeMarkdown>)
    const td = document.querySelector('td')
    expect(td?.className).toContain('tabular-nums')
  })

  it('链接协议仍然被清洗（安全不回退）', () => {
    render(<SafeMarkdown>{'[x](javascript:alert(1))'}</SafeMarkdown>)
    const a = document.querySelector('a')
    if (a) expect(a.getAttribute('href') || '').not.toContain('javascript:')
  })
})
