/**
 * L3-G / G14 钉子（2026-10-24）：卡片标题用语义标题元素
 *
 * 审计实测：首页 10+ 个模块只有 2 个 h1/h2，其余都是 div/span ——
 * 读屏与键盘用户无法按标题跳转。改标签不改视觉（className 原样保留）。
 */
import { readFileSync } from 'fs'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

const SRC = readFileSync(join(__dirname, '../../src/pages/Dashboard.tsx'), 'utf8')

describe('卡片标题语义化', () => {
  it('大盘资金流用 h2，且保留原 className', () => {
    expect(SRC).toContain('<h2 className="text-[13px] font-semibold">{t(\'dashboard.fundflow\')}</h2>')
  })

  it('市场温度用 h2', () => {
    expect(SRC).toContain('<h2 className="text-[13px] font-semibold">市场温度</h2>')
  })

  it('不再用 span 承担这两个卡片标题', () => {
    expect(SRC).not.toContain('<span className="text-[13px] font-semibold">{t(\'dashboard.fundflow\')}</span>')
    expect(SRC).not.toContain('<span className="text-[13px] font-semibold">市场温度</span>')
  })
})
