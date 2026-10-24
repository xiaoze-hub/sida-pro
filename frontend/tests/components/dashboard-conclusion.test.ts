/**
 * L2-A 首页结论层钉子（2026-10-24）
 *
 * 审计 B1 的核心问题不是"排版不好看"，而是**首屏读完说不出结论**：
 * 8 个数字并排、没有任何一句话。这里锁住"结论层必须存在且带非数字句子"。
 *
 * 未达成的部分（第 2 步做）：首屏数字 ≤9 —— 需要把 5 张指数卡收敛成数字+方向，
 * 属于证据层改造，不在本条断言范围内。
 */
import { readFileSync } from 'fs'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

const SRC = readFileSync(join(__dirname, '../../src/pages/Dashboard.tsx'), 'utf8')

describe('首屏结论层（B1）', () => {
  it('结论层存在，且用强边框与后面内容分层', () => {
    expect(SRC).toContain('data-testid="market-conclusion"')
    expect(SRC).toMatch(/data-testid="market-conclusion"[\s\S]{0,120}border-border-strong/)
  })

  it('有一句非数字结论（含主线与红绿盘判断）', () => {
    expect(SRC).toContain('主线在')
    expect(SRC).toContain('红盘占优')
    expect(SRC).toContain('绿盘占优')
  })

  it('缺数时写「未取到 / 尚未识别」，不用 0 顶', () => {
    expect(SRC).toContain('涨跌家数未取到')
    expect(SRC).toContain('尚未识别')
  })

  it('3 个结论数字用 20px + 等宽数字，而不是 12px 灰字', () => {
    const block = SRC.slice(SRC.indexOf('data-testid="market-conclusion"'), SRC.indexOf('核心接口失败横幅'))
    expect((block.match(/font-num text-\[20px\] font-semibold tabular-nums/g) || []).length).toBeGreaterThanOrEqual(3)
  })

  it('细项（封板率/成交/净流入）降为第二行小字，不再与结论同权', () => {
    const block = SRC.slice(SRC.indexOf('data-testid="market-conclusion"'), SRC.indexOf('核心接口失败横幅'))
    expect(block).toContain('border-t border-border/40 pt-2')
    expect(block).toContain('text-[11px]')
  })
})
