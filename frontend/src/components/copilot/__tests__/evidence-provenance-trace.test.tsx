// @vitest-environment jsdom
/**
 * P1 回答证据可追溯（2026-10-29）—— **只断言真实存在的结构证据**。
 *
 * 核实过的事实（基线 1537a081）：
 *  - `chat_messages` 表只有 id / conversation_id / role / content / created_at，**没有 run_id**
 *    （`src/db/models.py:1384`）；SSE `done` 帧只回 `{id, role, content, created_at}`
 *    （`src/web/api/chat.py:2284`）。
 *  - 因此「这条助手消息 ↔ 哪一轮 run」目前**只能按提问文本匹配**（`lib/copilot/match-run.ts`），
 *    这是推断，不是服务端关联 —— 任何 UI 都不许把它画成已核验。
 *  - 每条依据（逐句）与某次工具结果之间**没有任何 ID/序号关联**：`CopilotRunStep` 只有
 *    seq/tool/ok/elapsed_ms/args/result_digest，回答正文没有引用任何 step。
 *    所以逐条出处恒为「无」，不允许按正则把某句话「对到」某个工具。
 *  - 真实的逐条取数痕迹只有 `stage` 流折出来的齐套度（`evidence-readiness.ts`），
 *    它属于本条消息自己的流，可以算已核验；但它说明的是「哪几路取了」，不是「哪一句来自哪一路」。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { EvidenceTraceNote } from '@/components/copilot/EvidenceTraceNote'
import { EvidenceReadinessStrip } from '@/components/copilot/EvidenceReadinessStrip'
import InlineSteps from '@/components/copilot/InlineSteps'
import {
  associationLabel,
  buildAnswerTrace,
  type TraceRun,
} from '@/lib/copilot/evidence-provenance'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import type { ReadinessState } from '@/lib/copilot/evidence-readiness'
import type { CopilotRunRow, CopilotRunStep } from '@/lib/copilot/api'

beforeEach(() =>
  localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: 'test-user' }))}.signature`),
)
afterEach(() => {
  localStorage.removeItem('token')
  cleanup()
  sessionStorage.clear()
})

const RUN: TraceRun = {
  run_id: 'run-abc',
  created_at: '2026-10-29T09:00:00',
  status: 'ok',
  tool_calls: 2,
  elapsed_ms: 12400,
  question: '看看 600111',
}

const STEPS: CopilotRunStep[] = [
  { seq: 1, tool: 'get_stock_quote', ok: true, elapsed_ms: 320, args: null, result_digest: 'd1' },
  { seq: 2, tool: 'get_delta_series', ok: true, elapsed_ms: 800, args: null, result_digest: 'd2' },
  { seq: 3, tool: 'get_stock_quote', ok: false, elapsed_ms: 60, args: null, result_digest: null },
]

const READINESS: ReadinessState = {
  items: [
    { key: 'get_stock_quote', label: '行情', group: '走势', status: 'ok' },
    { key: 'get_delta_series', label: '逐笔', group: '主力', status: 'unknown' },
    { key: 'get_cancel_order_intent', label: '撤单', group: '筹码', status: 'failed' },
  ],
  done: 1,
  total: 3,
  failed: 1,
  unknown: 1,
}

describe('① buildAnswerTrace：只收真实字段，缺就不编', () => {
  it('没有 run：association 归 none、不核验、时间/步数/缺口全 null', () => {
    const t = buildAnswerTrace({ run: null, steps: null, readiness: null })
    expect(t.association).toBe('none')
    expect(t.verified).toBe(false)
    expect(t.runId).toBeNull()
    expect(t.createdAt).toBeNull()
    expect(t.stepCount).toBeNull()
    expect(t.tools).toEqual([])
    expect(t.failedCount).toBeNull()
    expect(t.unknownCount).toBeNull()
    // 逐条/逐句来源恒为无：当前链路没有这个关联
    expect(t.rowLevel).toBe(false)
  })

  it('没 run 却硬塞 association=run-id：一律降级成 none（不许无凭证自称已核验）', () => {
    const t = buildAnswerTrace({ run: null, association: 'run-id' })
    expect(t.association).toBe('none')
    expect(t.verified).toBe(false)
  })

  it('按提问文本匹配：verified=false，且不把 run.tool_calls 当成已加载步数', () => {
    const t = buildAnswerTrace({ run: RUN, steps: undefined, association: 'question-text' })
    expect(t.verified).toBe(false)
    expect(t.association).toBe('question-text')
    expect(t.runId).toBe('run-abc')
    expect(t.createdAt).toBe('2026-10-29T09:00:00')
    // 步骤没加载 → null，不能写 2（那是 run.tool_calls 的自报值）
    expect(t.stepCount).toBeNull()
    expect(t.tools).toEqual([])
  })

  it('steps 已加载：条数取真实明细，工具名保序去重', () => {
    const t = buildAnswerTrace({ run: RUN, steps: STEPS, association: 'run-id' })
    expect(t.verified).toBe(true)
    expect(t.stepCount).toBe(3)
    expect(t.tools).toEqual(['get_stock_quote', 'get_delta_series'])
  })

  it('steps 是空数组（已加载、本轮零工具）→ 0，不是 null', () => {
    const t = buildAnswerTrace({ run: RUN, steps: [], association: 'run-id' })
    expect(t.stepCount).toBe(0)
    expect(t.tools).toEqual([])
  })

  it('缺口计数来自真实齐套度；没有取数痕迹时是 null（不是 0）', () => {
    const withReady = buildAnswerTrace({ run: RUN, readiness: READINESS })
    expect(withReady.failedCount).toBe(1)
    expect(withReady.unknownCount).toBe(1)

    const empty: ReadinessState = { items: [], done: 0, total: null, failed: 0, unknown: 0 }
    const none = buildAnswerTrace({ run: RUN, readiness: empty })
    expect(none.failedCount).toBeNull()
    expect(none.unknownCount).toBeNull()
  })

  it('关联方式文案：只有 run-id 不说「未验证」', () => {
    expect(associationLabel('run-id')).toBe('按 run_id 关联')
    expect(associationLabel('question-text')).toContain('未验证')
    expect(associationLabel('none')).toContain('未关联')
  })
})

describe('② EvidenceTraceNote：分层说明，不把文本匹配说成来源', () => {
  it('文本匹配的 run：显式标未验证，并说明逐句来源不可查', () => {
    const trace = buildAnswerTrace({ run: RUN, steps: STEPS, association: 'question-text' })
    render(<EvidenceTraceNote trace={trace} rowCount={2} />)

    const root = screen.getByTestId('copilot-evidence-trace')
    expect(root.getAttribute('data-verified')).toBe('false')

    const runRow = screen.getByTestId('evidence-trace-run')
    expect(runRow.textContent).toContain('run-abc')
    expect(runRow.textContent).toContain('2026-10-29T09:00:00')
    expect(runRow.textContent).toContain('未验证')

    // 逐句/逐条来源：必须明说「无」，不能给出任何工具↔句子的对应
    const rows = screen.getByTestId('evidence-trace-rows')
    expect(rows.textContent).toContain('2')
    expect(rows.textContent).toContain('无逐句来源')
  })

  it('已核验关联：不说「未验证」，但仍不声称逐句可追溯', () => {
    const trace = buildAnswerTrace({ run: RUN, steps: STEPS, association: 'run-id' })
    render(<EvidenceTraceNote trace={trace} />)
    const runRow = screen.getByTestId('evidence-trace-run')
    expect(runRow.textContent).toContain('按 run_id 关联')
    expect(runRow.textContent).not.toContain('未验证')
    expect(screen.getByTestId('evidence-trace-rows').textContent).toContain('无逐句来源')
  })

  it('工具只列真实步骤里出现过的，并声明顺序不对应依据条文', () => {
    const trace = buildAnswerTrace({ run: RUN, steps: STEPS, association: 'question-text' })
    render(<EvidenceTraceNote trace={trace} />)
    const tools = screen.getByTestId('evidence-trace-tools')
    expect(tools.textContent).toContain('get_stock_quote')
    expect(tools.textContent).toContain('get_delta_series')
    expect(tools.textContent).toContain('不对应')
  })

  it('步骤未加载：不出现工具行，也不写 0 步', () => {
    const trace = buildAnswerTrace({ run: RUN, steps: undefined, association: 'question-text' })
    render(<EvidenceTraceNote trace={trace} />)
    expect(screen.queryByTestId('evidence-trace-tools')).toBeNull()
    expect(screen.getByTestId('evidence-trace-run').textContent).toContain('步骤未加载')
  })

  it('没有 run：说清没关联，不显示任何 run_id', () => {
    const trace = buildAnswerTrace({ run: null })
    render(<EvidenceTraceNote trace={trace} />)
    const runRow = screen.getByTestId('evidence-trace-run')
    expect(runRow.textContent).toContain('未关联')
    expect(screen.queryByTestId('evidence-trace-tools')).toBeNull()
  })

  it('缺口：有真实齐套度才写数字，没有就说没有取数痕迹', () => {
    const withReady = buildAnswerTrace({ run: RUN, readiness: READINESS })
    const { unmount } = render(<EvidenceTraceNote trace={withReady} />)
    const gaps = screen.getByTestId('evidence-trace-gaps')
    expect(gaps.textContent).toContain('未取到 1')
    expect(gaps.textContent).toContain('未确认 1')
    unmount()

    const noReady = buildAnswerTrace({ run: RUN, readiness: null })
    render(<EvidenceTraceNote trace={noReady} />)
    expect(screen.getByTestId('evidence-trace-gaps').textContent).toContain('无取数痕迹')
  })
})

describe('③ AnswerCard 接线：不传 trace 就不多一行（老调用点零变化）', () => {
  const RAW = `【结论】观望，等量能确认
【依据】
- 今日 -1.9%（腾讯行情）
- 底仓成本 40.16 元（自算CYQ口径）
【预案】继续保持观察
【口径】普通行情
以上分析不构成投资建议。`

  it('不传 trace：不渲染溯源条', () => {
    render(<AnswerCard parsed={parseAnswer(RAW)} />)
    expect(screen.queryByTestId('copilot-evidence-trace')).toBeNull()
  })

  it('传 trace：溯源条出现，且落在依据层之后', () => {
    const trace = buildAnswerTrace({ run: RUN, steps: STEPS, association: 'question-text' })
    render(<AnswerCard parsed={parseAnswer(RAW)} trace={trace} />)
    const note = screen.getByTestId('copilot-evidence-trace')
    expect(note.getAttribute('data-verified')).toBe('false')
    expect(screen.getByTestId('copilot-answer-evidence')).toBeTruthy()
  })

  it('依据自带的来源角标是模型自述：title 必须说明未核验', () => {
    render(<AnswerCard parsed={parseAnswer(RAW)} />)
    const chips = screen.getAllByTestId('copilot-evidence-source')
    expect(chips.length).toBeGreaterThan(0)
    chips.forEach((c) => expect(c.getAttribute('title') || '').toContain('模型自述'))
  })
})

describe('④ InlineSteps：默认按提问文本匹配，要标未验证', () => {
  const run: CopilotRunRow = {
    run_id: 'r1',
    conversation_id: 7,
    recipe: 'market_attribution',
    status: 'ok',
    tool_calls: 2,
    elapsed_ms: 3200,
    question: '看看 600111',
    created_at: '2026-10-29T09:00:00',
  }

  it('默认（只给了按文本匹配到的 run）→ 出现「未验证关联」', () => {
    render(<InlineSteps run={run} />)
    const badge = screen.getByTestId('inline-steps-association')
    expect(badge.textContent).toContain('未验证')
    expect(badge.getAttribute('data-association')).toBe('question-text')
  })

  it('显式 run-id 关联 → 不出现「未验证」', () => {
    render(<InlineSteps run={run} association="run-id" />)
    const badge = screen.getByTestId('inline-steps-association')
    expect(badge.textContent).not.toContain('未验证')
    expect(badge.getAttribute('data-association')).toBe('run-id')
  })

  it('展开后的摘要文案不受影响（仍是「共 N 步 / 失败 M 步」）', () => {
    const steps = [
      { seq: 1, tool: 'get_stock_quote', ok: true, elapsed_ms: 320, args: null, result_digest: null },
      { seq: 2, tool: 'get_margin_trading', ok: false, elapsed_ms: 5000, args: null, result_digest: null },
    ]
    render(<InlineSteps run={run} steps={steps} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    const summary = screen.getByTestId('inline-steps-summary')
    expect(summary.textContent).toContain('共 2 步')
    expect(summary.textContent).toContain('失败 1 步')
  })
})

describe('⑤ 齐套度条：标明来源是本条流的 stage 事件（唯一已核验的一层）', () => {
  it('容器带 data-provenance，进度文案 title 说明「已核验但非逐句出处」', () => {
    render(<EvidenceReadinessStrip items={READINESS.items} done={1} total={3} failed={1} />)
    const root = screen.getByTestId('evidence-readiness')
    expect(root.getAttribute('data-provenance')).toBe('stream-stage')
    const progress = screen.getByTestId('evidence-readiness-progress')
    const title = progress.getAttribute('title') || ''
    expect(title).toContain('已核验')
    expect(title).toContain('逐句')
  })
})
