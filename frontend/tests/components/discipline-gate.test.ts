// 反人性纪律闸门 (DisciplineCoolDownBanner + DefenseLineBanner) 契约测试
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'fs'
import { resolve } from 'path'

const fileWorkbench = readFileSync(
  resolve(__dirname, '../../src/pages/StockWorkbench.tsx'),
  'utf-8',
)

const fileCoolDown = readFileSync(
  resolve(__dirname, '../../src/components/DisciplineCoolDownBanner.tsx'),
  'utf-8',
)

const fileDefense = readFileSync(
  resolve(__dirname, '../../src/components/DefenseLineBanner.tsx'),
  'utf-8',
)

const fileVerdict = readFileSync(
  resolve(__dirname, '../../src/components/ThreeTierVerdictRow.tsx'),
  'utf-8',
)

describe('反人性纪律闸门契约', () => {
  it('StockWorkbench 挂载冷水舱警示条与持仓硬防线警报', () => {
    expect(fileWorkbench).toMatch(/import \{ DisciplineCoolDownBanner \} from '@\/components\/DisciplineCoolDownBanner'/)
    expect(fileWorkbench).toMatch(/import \{ DefenseLineBanner \} from '@\/components\/DefenseLineBanner'/)
    expect(fileWorkbench).toMatch(/<DisciplineCoolDownBanner \/>/)
    expect(fileWorkbench).toMatch(/<DefenseLineBanner symbol=\{symbol\} cost=\{position\.cost\} hasPosition=\{hasPosition\} \/>/)
  })

  it('DisciplineCoolDownBanner 绑定大盘退潮/冰点期判定', () => {
    expect(fileCoolDown).toMatch(/\/market\/phase/)
    expect(fileCoolDown).toMatch(/phase === 'ebb'/)
    expect(fileCoolDown).toMatch(/phase === 'ice'/)
    expect(fileCoolDown).toMatch(/冷静期警示/)
  })

  it('DefenseLineBanner 包含预设硬防线逻辑与纪律止损警报', () => {
    expect(fileDefense).toMatch(/\/quotes\//)
    expect(fileDefense).toMatch(/cost \* 0\.92/) // -8% 硬防线
    expect(fileDefense).toMatch(/触发硬防线警报 · 立即执行纪律/)
    expect(fileDefense).toMatch(/纪律高于预测/)
  })

  it('ThreeTierVerdictRow 嵌入三层滤网行动禁令约束', () => {
    expect(fileVerdict).toMatch(/three-tier-discipline-constraint/)
    expect(fileVerdict).toMatch(/退潮期严禁盲目加仓/)
    expect(fileVerdict).toMatch(/滞涨背离禁止追高/)
    expect(fileVerdict).toMatch(/弹簧待确认·切勿抢跑/)
  })
})
