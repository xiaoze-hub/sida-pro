/**
 * P2-3 钉子(前端): 能力矩阵要能回答"降级影响我哪个页面 / 看到的是缺的还是有替代"。
 *
 * 三条不许退: ① 只有**非 ok** 的能力才出现"影响面"入口(正常行保持可扫);
 *            ② 展开后必须三行齐: 受影响页面 / 降级时表现 / 替代源;
 *            ③ 没登记受影响页面时显式写"未登记", 不冒充"无影响"。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../..')
const comp = readFileSync(resolve(root, 'src/components/DataCapabilities.tsx'), 'utf-8')
const lib = readFileSync(resolve(root, 'src/lib/data-capabilities.ts'), 'utf-8')

describe('能力矩阵: 影响面', () => {
  it('类型里有 impact(pages/effect/fallback)', () => {
    expect(lib).toContain('interface CapabilityImpact')
    for (const f of ['pages', 'effect', 'fallback']) {
      expect(lib).toContain(f)
    }
    expect(lib).toContain('impact?:')
  })

  it('只在非 ok 时给入口(正常行不加噪)', () => {
    expect(comp).toMatch(/item\.status !== 'ok' && !!imp/)
    expect(comp).toContain('data-testid="impact-toggle"')
  })

  it('展开后三行齐, 且缺页面时写"未登记"', () => {
    expect(comp).toContain('受影响页面')
    expect(comp).toContain('降级时')
    expect(comp).toContain('替代源')
    expect(comp).toMatch(/imp\.pages\.length \? imp\.pages\.join\(' · '\) : '未登记'/)
  })

  it('文案不带 markdown 星号', () => {
    const code = comp.split('\n').filter((l) => !/^\s*(\*|\/\*|\/\/)/.test(l)).join('\n')
    expect(code).not.toMatch(/>[^<]{0,80}\*\*/)
  })
})
