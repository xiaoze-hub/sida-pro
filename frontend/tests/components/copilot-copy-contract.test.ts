/**
 * 参谋文案合规契约（设计稿 §7.4）。
 *
 * 只扫 `src/lib/copilot/copy.ts` —— 所有面向用户的静态文案都收敛在那一个文件里，
 * 一条正则就能拦住整站的禁用词回归。动态文案（后端 verdict）由后端单测负责。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = readFileSync(resolve(__dirname, '../../src/lib/copilot/copy.ts'), 'utf-8')
const FORBIDDEN = /建议买入|建议卖出|主力建仓|主力出货|必涨|稳赚|抄底必赚|满仓|梭哈/

describe('参谋文案合规', () => {
  it('不含禁用词', () => {
    const hits = SRC.match(FORBIDDEN)
    expect(hits, `命中禁用词: ${hits}`).toBeNull()
  })

  it('带风险提示锚点（每张卡/每个触点都必须能拿到这一句）', () => {
    expect(SRC).toMatch(/不构成投资建议/)
  })

  it('底仓成本口径用「底仓成本」，不写「建仓成本」', () => {
    expect(SRC).not.toMatch(/建仓成本/)
  })

  it('缺失语义三态都有独立文案（不许一律 -- 了事）', () => {
    expect(SRC).toMatch(/升级/)
    expect(SRC).toMatch(/暂无/)
    expect(SRC).toMatch(/不猜价位/)
  })
})
