/**
 * B7 钉子(前端): 内部计量看板的**耗时**视图不许把"没样本"显示成"0 ms"。
 *
 * 由来: 耗时是最容易"看起来合理其实在编"的数字 —— `null`(没有已记录耗时的调用)
 * 一旦被渲染成 `0 ms`, 读者会以为"这接口很快", 实际是"我们从没记过"。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const raw = readFileSync(resolve(__dirname, '../../src/pages/Admin.tsx'), 'utf-8')
/** 去掉注释: 注释里写"不许出现 X"是说明, 不算违规(否则检查会逼着人不解释)。 */
const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

describe('用量看板 · 耗时视图', () => {
  it('取的是耗时端点(与"按 Key 数次数"那张表分开)', () => {
    expect(src).toContain('/admin/skills/usage/latency?days=7')
  })

  it('p50/p95 为 null 时显示 --, 不是 0 ms', () => {
    expect(src).toMatch(/r\.p50_ms === null \? '--' : `\$\{r\.p50_ms\} ms`/)
    expect(src).toMatch(/r\.p95_ms === null \? '--' : `\$\{r\.p95_ms\} ms`/)
    // 不允许出现"null 就当 0"的写法
    expect(src).not.toMatch(/p50_ms \?\? 0/)
    expect(src).not.toMatch(/p95_ms \?\? 0/)
  })

  it('把"为什么是 --"放在单元格 title 上(读者不用猜)', () => {
    expect(src).toContain('没有已记录耗时的调用, 无法计算')
  })

  it('口径说明由后端给(latency_note), 页面不自己措辞', () => {
    expect(src).toContain('usageLatency.latency_note')
    expect(src).not.toContain('未记录 ≠ 0ms')   // 这句话只应来自后端
  })

  it('显示样本数(分母可见, 免得拿 1 个样本的 p95 当结论)', () => {
    expect(src).toContain('latency_samples')
  })
})
