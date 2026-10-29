// @vitest-environment jsdom
/**
 * 回答审查修复（2026-10-29，第二轮）。
 *
 * 审查在上一轮的「风险 / 数据缺口 / 口径」分层里复现了四类真问题，本文件逐条钉住：
 *  ① **多处【风险】只取第一段**：模型在一份回答里写了两段风险（各自带【风险】/【风险提示】
 *    标记）时，只有第一段进了风险块，第二段被 `stripRiskTail` 从「标记到块尾」整段删掉 ——
 *    用户看不见，原文也没了。
 *  ② **风险里的【002594】被当成下一段开头**：股票代码、模型自己写的方括号术语不是分节标记，
 *    却按 `【[^】]{1,14}】` 一刀切，导致这条风险之后的内容全部丢失。
 *  ③ **markdown 标题被当成风险文字**：`### 失效条件`、`### 数据缺口` 这种标题成了风险条目，
 *    标题底下的「暂无 / 未取到」条目又被算成风险（缺失数据错归风险）。
 *  ④ **不实文案**：风险标题写「风险与失效条件」（原文没有失效条件也算）、缺口说明写
 *    「未参与上述结论」（回答里根本没法验证）、InlineSteps 在 run 状态 error 但零失败步骤时
 *    说「全部成功」。
 *
 * 铁律：**任何内容都不许丢**。宁可这一条不提取、退回原文整段展示，也不能吞字。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import InlineSteps from '@/components/copilot/InlineSteps'
import {
  extractGapLines,
  extractRiskLines,
  partitionEvidence,
  stripRiskTail,
} from '@/components/copilot/answer-sections'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import type { CopilotRunRow } from '@/lib/copilot/api'

beforeEach(() =>
  localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: 'test-user' }))}.signature`),
)
afterEach(() => {
  localStorage.removeItem('token')
  cleanup()
  sessionStorage.clear()
})

/** 两段风险：第一段在【依据】后，第二段被模型写到了【预案】后面。 */
const RAW_MULTI_RISK = `【结论】洗盘偏多，站稳 32.80 后再看，置信度 6/10
【依据】
- 逐笔主力净流出 552 万（腾讯逐笔口径）
- 底仓成本 40.16 元（自算CYQ口径）
【风险】
- 跌破 32.80 视为失效
- 量能萎缩则结论不成立
【预案】持仓者：跌破 33.20 元后重新评估
【风险提示】
- 【002594】比亚迪同步走弱则一起失效
- 研报数据未取到
【口径】腾讯逐笔 + 自算CYQ
以上分析仅供参考，不构成投资建议。`

/** 风险段里套 markdown 标题，且标题下跟着「数据缺口」段。 */
const RAW_HEADING_RISK = `【结论】洗盘偏多，置信度 6/10
【依据】
- 逐笔主力净流出 552 万（腾讯逐笔口径）
【风险】
### 失效条件
- 跌破 32.80 视为失效
### 数据缺口
- 研报数据未取到
【预案】持仓者：跌破 33.20 元后重新评估
【口径】腾讯逐笔`

describe('① 多处【风险】：每一段都要提取，不许截删', () => {
  it('两段风险的全部条目都在（含第二段）', () => {
    const parsed = parseAnswer(RAW_MULTI_RISK)
    expect(extractRiskLines(parsed.raw)).toEqual([
      '跌破 32.80 视为失效',
      '量能萎缩则结论不成立',
      '【002594】比亚迪同步走弱则一起失效',
    ])
  })

  it('卡片里两段风险都渲染出来，且预案里不再重复风险文字', () => {
    const parsed = parseAnswer(RAW_MULTI_RISK)
    render(<AnswerCard parsed={parsed} />)

    const risk = screen.getByTestId('copilot-answer-risk')
    expect(risk.textContent).toContain('跌破 32.80 视为失效')
    expect(risk.textContent).toContain('量能萎缩则结论不成立')
    expect(risk.textContent).toContain('【002594】比亚迪同步走弱则一起失效')
    expect(risk.textContent).toContain('3 条')

    const plan = screen.getByTestId('copilot-answer-plan')
    expect(plan.textContent).toContain('跌破 33.20 元后重新评估')
    expect(plan.textContent).not.toContain('002594')
  })
})

describe('② 风险里的【002594】不是分节标记', () => {
  it('代码方括号不会把风险段劈断（后面的条目一条不丢）', () => {
    const raw = `【依据】
- 主力净流出 552 万
【风险】
- 【002594】比亚迪同步走弱则一起失效
- 跌破 32.80 视为失效
【预案】持仓者继续观察`
    expect(extractRiskLines(raw)).toEqual([
      '【002594】比亚迪同步走弱则一起失效',
      '跌破 32.80 视为失效',
    ])
  })

  it('stripRiskTail 只删风险段本身，后面的【预案】原文要留着', () => {
    expect(
      stripRiskTail('持仓者：等回踩 【风险】跌破 32.80 失效 【预案】空仓者：等待 30.00 元'),
    ).toBe('持仓者：等回踩\n【预案】空仓者：等待 30.00 元')
  })
})

describe('③ markdown 标题不是风险文字，缺失数据不算风险', () => {
  it('标题行被跳过，风险条目只剩真正的失效条件', () => {
    const raw = `【风险】
### 失效条件
- 跌破 32.80 视为失效
【预案】持仓者继续观察`
    expect(extractRiskLines(raw)).toEqual(['跌破 32.80 视为失效'])
  })

  it('风险段后面的「数据缺口」标题与条目不算风险，也不进依据', () => {
    const parsed = parseAnswer(RAW_HEADING_RISK)
    render(<AnswerCard parsed={parsed} />)

    const risk = screen.getByTestId('copilot-answer-risk')
    expect(risk.textContent).toContain('跌破 32.80 视为失效')
    expect(risk.textContent).not.toContain('数据缺口')
    expect(risk.textContent).not.toContain('研报数据未取到')
    expect(risk.textContent).not.toContain('###')

    const gaps = screen.getByTestId('copilot-answer-gaps')
    expect(gaps.textContent).toContain('研报数据未取到')
  })

  it('风险段里声明的缺失归缺口，不冒充失效条件', () => {
    const parsed = parseAnswer(RAW_MULTI_RISK)
    render(<AnswerCard parsed={parsed} />)
    expect(screen.getByTestId('copilot-answer-risk').textContent).not.toContain('研报数据未取到')
    expect(screen.getByTestId('copilot-answer-gaps').textContent).toContain('研报数据未取到')
  })

  it('extractGapLines 认得【数据缺口】段与 markdown 标题段', () => {
    expect(extractGapLines('【依据】\n- 今日 -1.9%\n【数据缺口】\n- 研报数据未取到\n【预案】继续观察')).toEqual([
      '研报数据未取到',
    ])
    expect(extractGapLines('【依据】\n- 今日 -1.9%\n### 数据缺口\n- 龙虎榜未取到\n【预案】继续观察')).toEqual([
      '龙虎榜未取到',
    ])
  })
})

describe('④ 一条内容都不许丢（宁可整段退回原文）', () => {
  const RAW_MESSY = `## 【结论】洗盘偏多，站稳 32.80 再看，置信度 6/10
【依据】
- 逐笔主力净流出 552 万（腾讯逐笔口径）
- 底仓成本 40.16 元（自算CYQ口径）
【风险】
### 失效条件
- 跌破 32.80 视为失效
【002594】比亚迪同步走弱则一起失效
【数据缺口】
- 研报数据未取到
【预案】持仓者：跌破 33.20 元后重新评估
【风险提示】量能萎缩则结论不成立
【口径】腾讯逐笔 + 自算CYQ
以上分析仅供参考，不构成投资建议。`

  it('原文每一行都在卡片上（风险 / 缺口 / 依据 / 预案 / 口径各归其位）', () => {
    const parsed = parseAnswer(RAW_MESSY)
    const { container } = render(<AnswerCard parsed={parsed} />)
    const text = (container.textContent || '').replace(/\s+/g, '')

    for (const line of [
      '洗盘偏多，站稳32.80再看',
      '逐笔主力净流出552万',
      '底仓成本40.16元',
      '跌破32.80视为失效',
      '【002594】比亚迪同步走弱则一起失效',
      '研报数据未取到',
      '跌破33.20元后重新评估',
      '量能萎缩则结论不成立',
      '自算CYQ',
      '不构成投资建议',
    ]) {
      expect(text).toContain(line.replace(/\s+/g, ''))
    }
  })

  it('依据里的真实读数不会被并进缺口条目', () => {
    const { data, gaps } = partitionEvidence([
      '底仓成本 40.16 元（自算CYQ口径） 【数据缺口】 - 研报数据未取到',
    ])
    expect(data).toEqual(['底仓成本 40.16 元（自算CYQ口径）'])
    expect(gaps).toEqual(['研报数据未取到'])
  })
})

describe('⑤ 不实文案', () => {
  it('风险标题只说「风险」，不替模型虚构「失效条件」', () => {
    const parsed = parseAnswer(`【结论】观望\n【依据】\n- 今日 -1.9%\n【风险】量能萎缩则结论不成立\n【预案】继续观察`)
    render(<AnswerCard parsed={parsed} />)
    const risk = screen.getByTestId('copilot-answer-risk')
    expect(risk.textContent).toContain('风险')
    expect(risk.textContent).not.toContain('失效条件')
  })

  it('缺口说明不再断言「未参与上述结论」', () => {
    const parsed = parseAnswer(`【结论】观望\n【依据】\n- 今日 -1.9%\n- 研报数据未取到\n【预案】继续观察`)
    render(<AnswerCard parsed={parsed} />)
    const gaps = screen.getByTestId('copilot-answer-gaps')
    expect(gaps.textContent).toContain('研报数据未取到')
    expect(gaps.textContent).not.toContain('未参与')
  })

  const run: CopilotRunRow = {
    run_id: 'r2',
    conversation_id: 7,
    recipe: 'market_attribution',
    status: 'ok',
    tool_calls: 2,
    elapsed_ms: 4000,
    question: 'q',
    created_at: '2026-10-29T09:00:00',
  }
  const okSteps = [
    { seq: 1, tool: 'get_stock_quote', ok: true, elapsed_ms: 320, args: null, result_digest: null },
    { seq: 2, tool: 'get_delta_series', ok: true, elapsed_ms: 500, args: null, result_digest: null },
  ]

  it('run 出错但零失败步骤：不许说「全部成功」', () => {
    render(<InlineSteps run={{ ...run, status: 'error' }} steps={okSteps} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    const summary = screen.getByTestId('inline-steps-summary')
    expect(summary.textContent).toContain('共 2 步')
    expect(summary.textContent).not.toContain('全部成功')
    expect(summary.textContent).toContain('error')
  })

  it('run 正常且零失败步骤：仍然说「全部成功」', () => {
    render(<InlineSteps run={run} steps={okSteps} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    expect(screen.getByTestId('inline-steps-summary').textContent).toContain('全部成功')
  })
})

describe('⑥ 真实模板形状：多标的段落不会被方括号劈开', () => {
  const RAW_TWO_SYMBOL = `【结论】两只票都偏洗盘，置信度 6/10
【依据】
- 【600519】贵州茅台：量比 1.02，主力净流出 1.2 亿（腾讯逐笔口径）
- 【002594】比亚迪：量比 0.86，主力净流出 3200 万（腾讯逐笔口径）
- 龙虎榜暂无数据
【风险】
- 【600519】跌破 1680 元视为失效
- 【002594】跌破 92.50 元视为失效
【预案】持仓者：跌破 1680 元后减半；空仓者：等回踩 92.50 元
【口径】腾讯逐笔`

  it('两条标的依据都在，两条风险都在，缺失归缺口', () => {
    const parsed = parseAnswer(RAW_TWO_SYMBOL)
    expect(extractRiskLines(parsed.raw)).toEqual([
      '【600519】跌破 1680 元视为失效',
      '【002594】跌破 92.50 元视为失效',
    ])

    render(<AnswerCard parsed={parsed} />)
    const evidence = screen.getByTestId('copilot-answer-evidence').textContent || ''
    expect(evidence).toContain('【600519】贵州茅台')
    expect(evidence).toContain('【002594】比亚迪')
    expect(evidence).toContain('2 条')
    expect(screen.getByTestId('copilot-answer-gaps').textContent).toContain('龙虎榜暂无数据')
    expect(screen.getByTestId('copilot-answer-risk').textContent).toContain('2 条')
  })
})

describe('⑦ 依据条目不重复、也不被误删', () => {
  it('预案里顺口提到某个读数，不能把这条读数从依据里删掉', () => {
    const parsed = parseAnswer(`【结论】观望
【依据】
- 主力净流出 552 万
- 量比 1.02
【预案】持仓者：若主力净流出 552 万则减仓
【口径】腾讯逐笔`)
    render(<AnswerCard parsed={parsed} />)

    const evidence = screen.getByTestId('copilot-answer-evidence').textContent || ''
    expect(evidence).toContain('主力净流出 552 万')
    expect(evidence).toContain('量比 1.02')
    expect(evidence).toContain('2 条')
    // 预案整行不再在依据里重复一遍
    expect(evidence).not.toContain('减仓')
    expect(screen.getByTestId('copilot-answer-plan').textContent).toContain('减仓')
  })
})

describe('⑧ 风险 / 缺口的小标题残渣不许让同一句话在依据里再出现一次', () => {
  const RAW_TITLE_MIX = `## 【结论】洗盘偏多，站稳 32.80 后再看，置信度 6/10
【依据】
- 逐笔主力净流出 552 万（腾讯逐笔口径）
- 底仓成本 40.16 元（自算CYQ口径）
- 研报数据未取到
【风险】
### 失效条件
- 跌破 32.80 视为失效
- 【002594】比亚迪同步走弱则一起失效
### 数据缺口
- 龙虎榜未取到
【预案】持仓者：跌破 33.20 元后重新评估
空仓者：等回踩 32.80 元
【风险提示】量能萎缩则结论不成立
【口径】腾讯逐笔 + 自算CYQ
以上分析仅供参考，不构成投资建议。`

  it('依据只剩两条真实读数，风险三条都在，缺口两条都在', () => {
    render(<AnswerCard parsed={parseAnswer(RAW_TITLE_MIX)} />)

    const evidence = screen.getByTestId('copilot-answer-evidence').textContent || ''
    expect(evidence).toContain('2 条')
    expect(evidence).toContain('逐笔主力净流出 552 万')
    expect(evidence).toContain('底仓成本 40.16 元')
    expect(evidence).not.toContain('比亚迪')
    expect(evidence).not.toContain('未取到')

    expect(screen.getByTestId('copilot-answer-risk').textContent).toContain('3 条')
    const gaps = screen.getByTestId('copilot-answer-gaps').textContent || ''
    expect(gaps).toContain('研报数据未取到')
    expect(gaps).toContain('龙虎榜未取到')
    expect(gaps).toContain('2 条')
  })
})
