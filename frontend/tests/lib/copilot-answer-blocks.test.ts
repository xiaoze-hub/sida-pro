/**
 * 参谋回答结构化解析（P1 排版契约）。
 *
 * 契约来源：`src/web/api/chat.py` 的 SYSTEM_PROMPT「参谋回答模板」，但真实模型输出
 * 会加 markdown 标题、写表格、把段名写成「口径汇总 / 实战作战预案」——
 * 所以样例里既有「模板态」，也有从生产环境抄回来的「真实态」。
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

/** 生产环境真实回答（002361，v0.19.6 实测）：markdown 标题 + 表格 + 变体段名 + 满屏加粗 */
const REAL = `# 【002361 全景深度档案分析】

---

## 【结论】

**空头趋势延续，主力持续派发中，短线防守为主。** 置信度 **4/10**（基于 7/9 项证据，2 项暂无数据）。

当前股价 8.92 元（-1.55%），均线空头排列 + MACD 死叉 + 主力净流出——**三线共振走坏**。

---

## 【依据】（逐条列真实数字与口径）

| 维度 | 关键读数 | 口径/备注 |
|------|---------|-----------|
| 实时价 | 8.92 元，-1.55%，量 45.78 万手 | 腾讯行情 |
| 主力逐笔 | **净流出 -3531 万** | 腾讯逐笔·主力意图口径 |
| 筹码（自算CYQ） | 底仓成本 10.37，获利盘 3.8% | 自算CYQ·120日 |

> **口径差异提示**：逐笔 -3531 万 vs L2 -1178 万，方向一致。

---

## 【预案】

### 持仓者
- **第一防线 8.66 元（支撑位）**：破位减仓 1/3，止损纪律优先；
- **第二防线 8.47 元**：跌破视为派发加速，清仓离场。

---

## 【口径汇总】

腾讯逐笔 + 自算CYQ + TQ L2`

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

  it('真实输出：markdown 标题/加粗/表格都要吃掉，变体段名也要认', () => {
    const p = parseAnswer(REAL)
    expect(p.structured).toBe(true)
    expect(p.confidence).toBe(4)
    // 裁决里不能露出 `**` 或 `##`
    expect(p.verdict).not.toContain('**')
    expect(p.verdict).not.toContain('##')
    expect(p.verdict).toContain('空头趋势延续')
    // 表格 → 条目；加粗被剥离
    expect(p.evidence.length).toBeGreaterThanOrEqual(4)
    expect(p.evidence[0]).toBe('实时价：8.92 元，-1.55%，量 45.78 万手（腾讯行情）')
    expect(p.evidence.join(' ')).not.toContain('**')
    // 【口径汇总】是变体段名，也要认出来
    expect(p.caliber).toContain('腾讯逐笔')
    // 预案保留结构（含破位数字），不带 markdown 星号
    expect(p.plan).toContain('第一防线 8.66 元')
    expect(p.plan).not.toContain('**')
  })

  it('结论的第二段并入依据，不能丢内容', () => {
    const p = parseAnswer(REAL)
    expect(p.evidence.some((e) => e.includes('三线共振走坏'))).toBe(true)
  })
})

describe('splitEvidence', () => {
  it('支持数字序号与圆点混排，续行并入上一条', () => {
    const items = splitEvidence('1. 第一条\n继续说明\n- 第二条\n· 第三条')
    expect(items).toEqual(['第一条 继续说明', '第二条', '第三条'])
  })
})
