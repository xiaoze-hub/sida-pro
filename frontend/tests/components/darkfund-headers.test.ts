/**
 * L3-B 钉子（2026-10-24）：暗盘资金表头单位口径
 *
 * 现场：列头写「主力净流入(万/亿)」而单元格是 `toAmountFromWan()` 产出的 `+15.99亿`。
 * 表头带「万/亿」会让读者把 `+15.99亿` 读成「15.99 万」或「15.99」，
 * 违反 AGENTS「金额=元，单位换算必须精确」。
 *
 * 约定：**表头只写指标名，单位由单元格带**。
 */
import { readFileSync } from 'fs'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

const ZH = readFileSync(join(__dirname, '../../src/locales/zh-CN.ts'), 'utf8')

describe('暗盘资金 / 战报表头', () => {
  it('列头不再出现「(万/亿)」', () => {
    expect(ZH).not.toContain("mainNet: '主力净流入(万/亿)'")
    expect(ZH).not.toContain("totalAmount: '总成交额(万/亿)'")
  })

  it('列头只写指标名', () => {
    expect(ZH).toContain("mainNet: '主力净流入'")
    expect(ZH).toContain("totalAmount: '总成交额'")
  })

  it('单位改为脚注/提示承担（footerLegend 仍在，且说明万元口径）', () => {
    const foot = ZH.match(/footerLegend:\s*'([^']*)'/g) || []
    expect(foot.length).toBeGreaterThan(0)
    expect(foot.some((s) => s.includes('万元'))).toBe(true)
  })
})
