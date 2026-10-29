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

  it('DefenseLineBanner 只认用户自设防线，不再默认 -8%（裁决 B）', () => {
    expect(fileDefense).toMatch(/\/quotes\//)
    // 2026-10-24 裁决 B: 纪律位只能来自用户自设 Position.defense_price。
    // 旧契约 `cost * 0.92` 已删除 —— 系统不再替用户假设 -8%，未设置就不告警。
    expect(fileDefense).not.toMatch(/cost \* 0\.92/)
    expect(fileDefense).toMatch(/defense_price/)
    expect(fileDefense).toMatch(/未设置防守线/)
    // 取价失败必须显式降级，禁止 return null 让告警静默消失
    expect(fileDefense).toMatch(/防守线无法计算/)
    // 纪律口吻保留，但主语换成「你」—— 旧文案「纪律高于预测」已被方案 B 的
    // 「防守线是你定的绝对价，跌到即提示执行纪律；不设则不告警」取代。
    // 注意：文案里的星号已按「JSX 里不许出现 markdown 星号」的门禁去掉（会原样渲染），
    // 所以这里断言纯文本，别再匹配带星号的旧串（2026-10-29 修死锁）。
    expect(fileDefense).toMatch(/防守线是你定的绝对价/)
    expect(fileDefense).toMatch(/不设则不告警/)
  })

  it('ThreeTierVerdictRow 嵌入三层滤网行动禁令约束', () => {
    expect(fileVerdict).toMatch(/three-tier-discipline-constraint/)
    expect(fileVerdict).toMatch(/退潮期严禁盲目加仓/)
    expect(fileVerdict).toMatch(/滞涨背离禁止追高/)
    expect(fileVerdict).toMatch(/弹簧待确认·切勿抢跑/)
  })
})
