/**
 * L3-C 钉子（2026-10-24）：决策账本阈值段默认折叠
 *
 * 现场：20+ 个阈值（生命线/弱势线/大牛线/更佳线…）以 11px 灰字一行铺在首屏，
 * 读起来像配置文件泄露，而真正的结论（命中率表）被挤到下面。
 *
 * 约定：阈值**一个字段都不删**，只把容器改成 details（默认收起）。
 */
import { readFileSync } from 'fs'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

const SRC = readFileSync(join(__dirname, '../../src/pages/DecisionLedger.tsx'), 'utf8')

describe('阈值段', () => {
  it('用 details 折叠，默认不展开', () => {
    const block = SRC.slice(SRC.indexOf('data-testid="thresholds"'), SRC.indexOf('data-testid="thresholds"') + 900)
    expect(block).toContain('<details>')
    expect(block).not.toContain('<details open')
  })

  it('summary 写明个数，读者知道里面有多少', () => {
    expect(SRC).toContain('个阈值，点击展开')
  })

  it('阈值内容与 title 明细仍在（不删字段）', () => {
    expect(SRC).toContain('当前阈值(只读)')
    expect(SRC).toContain("t.source === 'env' ? t.env : '内置默认 ' + t.default")
  })

  it('命中率表仍是首屏主体（阈值段在它之前、且已折叠）', () => {
    // 注意: 文件头注释里也出现过「命中率」, 必须锚定 h2 标题而不是裸字符串
    const th = SRC.indexOf('data-testid="thresholds"')
    const hit = SRC.indexOf('>命中率</h2>')
    expect(th).toBeGreaterThan(0)
    expect(hit).toBeGreaterThan(th)
  })
})
