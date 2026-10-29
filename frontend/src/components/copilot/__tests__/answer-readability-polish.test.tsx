// @vitest-environment jsdom
/**
 * 回答可读性打磨（2026-10-29）。
 *
 * 守六件事（都是「用户读一遍就能分清层次」的最小集）：
 *  ① **风险独立成层**：模型写了【风险】才出现风险块；没写就不出现（不靠正则替模型编风险，
 *     `【预案】严格执行风控` 这类合规句不得被当成风险条目）；
 *  ② **数据缺口独立成层**：回答里自己声明的「暂无 / 未取到 / 不可用」从依据里拆出来，
 *    一条都不丢，但不再和真实读数混排 —— 用户要能一眼看出哪些维度真的取到了；
 *  ③ **来源可读**：依据尾部的「（腾讯逐笔口径）」拆成独立的来源标记，正文只剩读数，
 *     原文一字不丢；不是「口径 / 来源 / 数据源」的括号（如「（32.80 元）」）留在正文里；
 *  ④ **口径可读**：口径按分隔符拆成条目 + 固定标签，不再是一整行糊在一起的字；
 *  ⑤ **不叠卡片**：回答内部不再嵌套 `card-surface` 卡片，改用 hairline 分节；
 *  ⑥ **证据状态与过程**：齐套度条把「未确认」也数出来（unknown ≠ ok，也≠未取到）；
 *     InlineSteps 非 ok 状态给中文口径、展开后给「共 N 步 / 失败 M 步」。
 *
 * 边界：模型自评不是置信度（见 compass-confidence-semantics），本文件不碰自评口径；
 * 支撑/阻力/止损/目标的语义与锚点也不碰（见 review-safety / evidence-strip-and-anchors）。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { EvidenceReadinessStrip } from '@/components/copilot/EvidenceReadinessStrip'
import InlineSteps from '@/components/copilot/InlineSteps'
import {
  extractRiskLines,
  partitionEvidence,
  splitEvidenceSource,
  stripRiskTail,
} from '@/components/copilot/answer-sections'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import type { ReadinessItem } from '@/lib/copilot/evidence-readiness'
import type { CopilotRunRow } from '@/lib/copilot/api'

beforeEach(() => localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: 'test-user' }))}.signature`))
afterEach(() => {
  localStorage.removeItem('token')
  cleanup()
  sessionStorage.clear()
})

const RAW_WITH_RISK = `【结论】洗盘偏多，站稳支撑 32.80 后再看，置信度 6/10
【依据】
- 逐笔主力净流出 552 万（腾讯逐笔口径）
- 底仓成本 40.16 元（自算CYQ口径）
【风险】跌破 32.80 视为失效；量能萎缩则结论不成立
【预案】持仓者：跌破 33.20 元后重新评估
【口径】腾讯逐笔 + 自算CYQ
以上分析仅供参考，不构成投资建议。`

const RAW_PLAIN = `【结论】观望，等量能确认
【依据】
- 今日 -1.9%
- 暂无研报数据
【预案】继续保持观察
【口径】普通行情
以上分析不构成投资建议。`

describe('① 风险层：模型没写就不出现，写了就单独成层', () => {
  it('没有【风险】段落时不渲染风险块（不靠正则替模型编风险）', () => {
    const parsed = parseAnswer(RAW_PLAIN)
    expect(extractRiskLines(parsed.raw)).toEqual([])

    render(<AnswerCard parsed={parsed} />)
    expect(screen.queryByTestId('copilot-answer-risk')).toBeNull()
    expect(screen.getByTestId('copilot-answer-card').textContent).not.toContain('风险与失效')
  })

  it('【预案】里的合规句「严格执行风控」不算风险条目', () => {
    const raw = `【结论】短线博弈\n【依据】\n- 止损位 30.00 元\n【预案】严格执行风控，不追高\n【口径】行情数据`
    const parsed = parseAnswer(raw)
    expect(extractRiskLines(parsed.raw)).toEqual([])
    render(<AnswerCard parsed={parsed} />)
    expect(screen.queryByTestId('copilot-answer-risk')).toBeNull()
  })

  it('有【风险】段落：风险块独立呈现，且不在依据里重复一遍', () => {
    const parsed = parseAnswer(RAW_WITH_RISK)
    expect(extractRiskLines(parsed.raw)).toEqual([
      '跌破 32.80 视为失效',
      '量能萎缩则结论不成立',
    ])

    render(<AnswerCard parsed={parsed} />)
    const risk = screen.getByTestId('copilot-answer-risk')
    expect(risk.textContent).toContain('跌破 32.80 视为失效')
    expect(risk.textContent).toContain('量能萎缩则结论不成立')

    const evidence = screen.getByTestId('copilot-answer-evidence')
    expect(evidence.textContent).not.toContain('视为失效')
    // 依据里的读数一条都不能少
    expect(evidence.textContent).toContain('逐笔主力净流出 552 万')
    expect(evidence.textContent).toContain('底仓成本 40.16 元')
  })

  it('风险文字不会漏进预案 / 口径 / 裁决（stripRiskTail 只截风险段）', () => {
    expect(stripRiskTail('持仓者：跌破 33.20 元后重新评估 【风险】跌破 32.80 视为失效')).toBe(
      '持仓者：跌破 33.20 元后重新评估',
    )
    expect(stripRiskTail('腾讯逐笔 + 自算CYQ')).toBe('腾讯逐笔 + 自算CYQ')
    expect(stripRiskTail('【风险提示】只是一句风险')).toBe('')
  })
})

describe('② 数据缺口层：声明的缺失从依据里拆出来，一条不丢', () => {
  it('「暂无 / 未取到 / 不可用」归入缺口，真实读数留在依据', () => {
    const evidence = [
      '今日 -1.9%',
      '暂无研报数据',
      '逐笔数据未取到',
      'L2 盘口不可用',
      '主力净流出 552 万',
    ]
    const { data, gaps } = partitionEvidence(evidence, [])
    expect(data).toEqual(['今日 -1.9%', '主力净流出 552 万'])
    expect(gaps).toEqual(['暂无研报数据', '逐笔数据未取到', 'L2 盘口不可用'])
  })

  it('缺口在卡片里独立成块，依据计数只算真实读数', () => {
    const parsed = parseAnswer(RAW_PLAIN)
    render(<AnswerCard parsed={parsed} />)

    const gaps = screen.getByTestId('copilot-answer-gaps')
    expect(gaps.textContent).toContain('暂无研报数据')
    expect(screen.getByTestId('copilot-answer-evidence').textContent).not.toContain('暂无研报数据')
    // 依据头只报真实读数的条数（1 条），缺口条数单独说
    expect(screen.getByTestId('copilot-answer-evidence').textContent).toContain('1 条')
    expect(gaps.textContent).toContain('1 条')
  })

  it('没有缺口时不渲染缺口块（空 ≠ 0 条）', () => {
    const parsed = parseAnswer(RAW_WITH_RISK)
    render(<AnswerCard parsed={parsed} />)
    expect(screen.queryByTestId('copilot-answer-gaps')).toBeNull()
  })
})

describe('③ 来源可读：尾部口径括号拆成来源标记，原文不丢', () => {
  it('「（腾讯逐笔口径）」拆出来做来源标记，正文只留读数', () => {
    expect(splitEvidenceSource('逐笔主力净流出 552 万（腾讯逐笔口径）')).toEqual({
      body: '逐笔主力净流出 552 万',
      source: '腾讯逐笔口径',
    })
    expect(splitEvidenceSource('主力净流出 552 万 (东财四档·资金流向口径)')).toEqual({
      body: '主力净流出 552 万',
      source: '东财四档·资金流向口径',
    })
  })

  it('不是口径/来源/数据源的括号原样留在正文（不猜）', () => {
    expect(splitEvidenceSource('阻力位 36.50 元面临套牢盘（近 20 日成交密集区）')).toEqual({
      body: '阻力位 36.50 元面临套牢盘（近 20 日成交密集区）',
      source: null,
    })
    expect(splitEvidenceSource('底仓成本 40.16 元')).toEqual({
      body: '底仓成本 40.16 元',
      source: null,
    })
  })

  it('卡片里来源是独立标记，正文与来源都还在（一字不丢）', () => {
    const parsed = parseAnswer(RAW_WITH_RISK)
    render(<AnswerCard parsed={parsed} />)

    const sources = screen.getAllByTestId('copilot-evidence-source')
    expect(sources.map((s) => s.textContent)).toEqual(['腾讯逐笔口径', '自算CYQ口径'])
    const evidence = screen.getByTestId('copilot-answer-evidence')
    expect(evidence.textContent).toContain('逐笔主力净流出 552 万')
    expect(evidence.textContent).toContain('腾讯逐笔口径')
    expect(evidence.textContent).toContain('底仓成本 40.16 元')
  })
})

describe('④ 口径可读：拆分条目 + 固定标签', () => {
  it('口径按分隔符拆成条目并带标签', () => {
    const parsed = parseAnswer(RAW_WITH_RISK)
    render(<AnswerCard parsed={parsed} />)

    const caliber = screen.getByTestId('copilot-answer-caliber')
    expect(caliber.textContent).toContain('口径')
    expect(caliber.textContent).toContain('腾讯逐笔')
    expect(caliber.textContent).toContain('自算CYQ')
    const parts = screen.getAllByTestId('copilot-caliber-part')
    expect(parts).toHaveLength(2)
    // 风险段不污染口径
    expect(caliber.textContent).not.toContain('风险')
  })

  it('模板末尾的免责句不再混进口径（卡片底部已有免责）', () => {
    const parsed = parseAnswer(RAW_WITH_RISK)
    // 免责句按 parseAnswer 的切段规则本来会落进【口径】块 —— 这里确认它没被当口径条目
    expect(parsed.caliber).toContain('不构成投资建议')
    render(<AnswerCard parsed={parsed} />)

    const caliber = screen.getByTestId('copilot-answer-caliber')
    expect(caliber.textContent).not.toContain('不构成投资建议')
    // 全文里免责只留底部那一处
    const card = screen.getByTestId('copilot-answer-card').textContent || ''
    expect(card.match(/不构成投资建议/g) || []).toHaveLength(1)
  })
})

describe('⑤ 不叠卡片：回答内部只有 hairline 分节', () => {
  it('正文各节不再是卡片，节与节之间用 border-t 分隔', () => {
    const parsed = parseAnswer(RAW_WITH_RISK)
    const { container } = render(<AnswerCard parsed={parsed} symbol="600111" />)

    const card = screen.getByTestId('copilot-answer-card')
    // 卡内只允许两块**仪表盘面板**（罗盘 / 多维度依据，各自组件自己拥有 surface）；
    // 回答自有的裁决/风险/依据/缺口/预案/口径一律走 hairline 分节，不再嵌套卡片。
    const allowed = new Set(['copilot-decision-compass', 'copilot-multi-dimension-evidence'])
    Array.from(card.querySelectorAll('.card-surface')).forEach((el) => {
      expect(allowed.has(el.getAttribute('data-testid') || '')).toBe(true)
    })
    // 分节自身更不许是卡片（否则又叠回去了）
    container.querySelectorAll('[data-answer-section]').forEach((s) => {
      expect(s.className).not.toContain('card-surface')
    })

    const sections = container.querySelectorAll('[data-answer-section]')
    expect(sections.length).toBeGreaterThanOrEqual(4)
    sections.forEach((s) => expect(s.className).toContain('border-t'))
  })
})

describe('⑥ 证据状态与过程：未确认要数出来，过程状态要人话', () => {
  const items: ReadinessItem[] = [
    { key: 'get_stock_quote', label: '行情', group: '走势', status: 'ok' },
    { key: 'get_delta_series', label: '逐笔', group: '主力', status: 'unknown' },
    { key: 'get_cancel_order_intent', label: '撤单', group: '筹码', status: 'failed' },
  ]

  it('unknown 单独计数（既不算已取，也不算未取到）', () => {
    render(<EvidenceReadinessStrip items={items} done={1} total={3} failed={1} />)
    const unknown = screen.getByTestId('evidence-readiness-unknown-count')
    expect(unknown.textContent).toContain('未确认 1')
    expect(screen.getByTestId('evidence-readiness').textContent).toContain('已取 1/3')
    expect(screen.getByTestId('evidence-readiness-failed-count').textContent).toContain('未取到 1')
  })

  it('没有 unknown 时不出现「未确认」计数', () => {
    render(
      <EvidenceReadinessStrip
        items={[items[0], items[2]]}
        done={1}
        total={2}
        failed={1}
      />,
    )
    expect(screen.queryByTestId('evidence-readiness-unknown-count')).toBeNull()
  })

  const run: CopilotRunRow = {
    run_id: 'r1',
    conversation_id: 7,
    recipe: 'market_attribution',
    status: 'degraded',
    tool_calls: 3,
    elapsed_ms: 12400,
    question: 'q',
    created_at: '2026-10-29T09:00:00',
  }

  it('非 ok 状态给中文口径（保留原始状态码，不藏）', () => {
    render(<InlineSteps run={run} />)
    const toggle = screen.getByTestId('inline-steps-toggle').textContent || ''
    expect(toggle).toContain('degraded')
    expect(toggle).toContain('部分降级')
  })

  it('展开后给出「共 N 步 / 失败 M 步」摘要，失败步骤仍逐条可见', () => {
    const steps = [
      { seq: 1, tool: 'get_stock_quote', ok: true, elapsed_ms: 320, args: null, result_digest: null },
      { seq: 2, tool: 'get_margin_trading', ok: false, elapsed_ms: 5000, args: null, result_digest: null },
    ]
    render(<InlineSteps run={run} steps={steps} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))

    const summary = screen.getByTestId('inline-steps-summary')
    expect(summary.textContent).toContain('共 2 步')
    expect(summary.textContent).toContain('失败 1 步')
    expect(screen.getAllByTestId('inline-step')).toHaveLength(2)
  })

  it('全部成功时不报「失败 0 步」这种噪音', () => {
    const steps = [
      { seq: 1, tool: 'get_stock_quote', ok: true, elapsed_ms: 320, args: null, result_digest: null },
    ]
    render(<InlineSteps run={{ ...run, status: 'ok' }} steps={steps} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    const summary = screen.getByTestId('inline-steps-summary')
    expect(summary.textContent).toContain('共 1 步')
    expect(summary.textContent).not.toContain('失败')
  })
})
