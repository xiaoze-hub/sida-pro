/**
 * B9 钉子(前端): 因子有效性页的三条诚实口径 —— 期数不足给 `--` 不是 0、参考值标"仅对照"、
 * 后端报错照实显示。另外钉住"前端不自己算 IC"。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const raw = readFileSync(resolve(__dirname, '../../src/pages/FactorIC.tsx'), 'utf-8')
/** 只看会渲染的代码: 注释里写"不许自己算"是说明, 不算违规。 */
const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

describe('因子有效性页 · 期数不足不是"无效"', () => {
  it('IC 为 null 时显示 --, 且悬停说明是**期数不足**(而不是 0)', () => {
    expect(src).toContain("text: '--'")
    expect(src).toContain('期数不足')
    expect(src).not.toMatch(/\.ic \?\? 0/)
  })

  it('期数下限与后端一致(IC ≥3, 样本外 ≥2)', () => {
    expect(src).toMatch(/const MIN_PERIODS = 3/)
    expect(src).toMatch(/const MIN_HOLDOUT = 2/)
  })

  it('显示期数分母, 免得拿 3 个期数的 IC 当结论', () => {
    expect(src).toContain('ic_periods')
    expect(src).toContain('holdout_periods')
  })
})

describe('因子有效性页 · 口径不许含糊', () => {
  it('pooled 明确标为"参考值/仅对照", 不当作决策口径', () => {
    expect(src).toContain('参考值')
    expect(src).toContain('只作对照')
    expect(src).toContain('ic_pooled')
  })

  it('样本外 IC 单独成列(过拟合判据)', () => {
    expect(src).toContain('样本外 IC')
  })

  it('数字格式化走 safe* (全仓纪律), 不裸用 toFixed', () => {
    expect(src).toContain("from '@/lib/format'")
    expect(src).not.toMatch(/\.toFixed\(/)
  })
})

describe('因子有效性页 · 失败照实显示', () => {
  it('后端 error 原样显示, 不装作"没数据"', () => {
    expect(src).toContain('data?.error')
    expect(src).toContain('后端计算失败')
  })

  it('前端不自己算 IC/IR(只显示后端给的字段)', () => {
    // 注意: "Spearman" 出现在**口径说明的 tooltip**里是合理的(解释 pooled 是什么口径),
    // 所以这里只禁"真的去算"——相关系数函数调用/均值聚合。
    // 判据是"有没有计算", 不是"函数叫什么名"(第一版按名字禁 ic/ir → 把自己的 icCell 也禁了)
    expect(src).not.toMatch(/(spearman|pearson|correlation)\s*\(/i)
    expect(src).not.toContain('reduce(')
    expect(src).not.toContain('Math.sqrt')
    expect(src).not.toMatch(/from '(d3|simple-statistics|alphalens)/)
  })
})
