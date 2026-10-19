/**
 * 参谋回答结构化解析（P1 排版契约）。
 *
 * 契约来源：`src/web/api/chat.py` 的 SYSTEM_PROMPT「参谋回答模板」四段：
 * 【结论】【依据】【预案】【口径】。前端只负责切段与渲染，不重写内容。
 */
import { describe, expect, it } from 'vitest'
import { extractConfidence, parseAnswer, splitEvidence } from '../../src/lib/copilot/answer-blocks'

const SAMPLE = `【结论】洗盘吸筹偏多，但上方套牢盘较重，短期以震荡消化为主，置信度 6/10
【依据】
- 逐笔口径主力净流出 552 万（超大单 -1667 万 / 大单 +1115 万）
- 底仓成本 40.16 元，现价 35.23 元低于底仓成本 12.28%
- 获利盘仅 8.3%，90% 筹码带 35.36–49.36 元
- GS 策略处于 S 区（已确认）
【预案】持仓者：跌破 33.96 元第一防守位后重新评估；空仓者：站上 36 元再观察
【口径】自算CYQ·120日 + 腾讯逐笔
以上分析仅供参考，不构成投资建议。`

describe('parseAnswer', () => {
  it('切出四段并把依据拆成条目', () => {
    const p = parseAnswer(SAMPLE)
    expect(p.structured).toBe(true)
    expect(p.verdict).toContain('洗盘吸筹偏多')
    expect(p.evidence).toHaveLength(4)
    expect(p.evidence[0]).toContain('552 万')
    expect(p.plan).toContain('持仓者')
    expect(p.caliber).toContain('自算CYQ')
  })

  it('置信度只从原文 x/10 提取，且不残留在结论里', () => {
    const p = parseAnswer(SAMPLE)
    expect(p.confidence).toBe(6)
    expect(p.verdict).not.toContain('置信度')
    expect(p.verdict).not.toContain('6/10')
  })

  it('没有 /10 时给 null，不猜一个数', () => {
    expect(extractConfidence('置信度较高，趋势向好')).toBeNull()
    expect(extractConfidence('置信度 7/10')).toBe(7)
  })

  it('免责声明单独取出，正文尾巴重复一遍', () => {
    const p = parseAnswer(SAMPLE)
    expect(p.disclaimer).toContain('不构成投资建议')
    expect(p.plan).not.toContain('不构成投资建议')
  })

  it('没按模板写的回答（闲聊/纯 markdown）必须回退原文', () => {
    const p = parseAnswer('今天两市成交 1.2 万亿，情绪回暖。')
    expect(p.structured).toBe(false)
    expect(p.raw).toContain('1.2 万亿')
  })

  it('空输入不炸', () => {
    const p = parseAnswer('')
    expect(p.structured).toBe(false)
    expect(p.evidence).toEqual([])
  })
})

describe('splitEvidence', () => {
  it('支持数字序号与圆点混排，续行并入上一条', () => {
    const items = splitEvidence('1. 第一条\n继续说明\n- 第二条\n· 第三条')
    expect(items).toEqual(['第一条 继续说明', '第二条', '第三条'])
  })
})
