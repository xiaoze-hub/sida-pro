// @vitest-environment jsdom
/**
 * EvidenceCard（证据卡）回归 —— 2026-10-25。
 *
 * 断言的是**可验证性**本身，不是样式偏好：
 *  1. 有数据时每一步都摊平渲染（序号 / 工具名 / 耗时 / ✓）；
 *  2. 失败步骤标红且**不隐藏**（失败是证据的一部分，藏起来等于伪造齐套）；
 *  3. 耗时缺失显示 `--`，**绝不补 0ms**；
 *  4. 无 step 时明说「本轮没有工具调用」，不装作加载完了；
 *  5. loading / error 两态各自给出诚实的文案（error 要带原因）；
 *  6. 截断口径：问题 40 字、args 60 字、digest 80 字，超出补省略号。
 *
 * 断言风格：只用 vitest 原生 matcher（不引 jest-dom）。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { EvidenceCard } from '../../src/components/copilot/EvidenceCard'
import type { EvidenceStep } from '../../src/components/copilot/EvidenceCard'

const RUN = {
  run_id: 'run-1',
  status: 'ok',
  recipe: 'auction_strong',
  tool_calls: 3,
  elapsed_ms: 4200,
  question: '金健米业竞价怎么看',
}

function makeStep(over: Partial<EvidenceStep> = {}): EvidenceStep {
  return {
    seq: 1,
    tool: 'get_auction_snapshot',
    ok: true,
    elapsed_ms: 820,
    args: '{"symbol":"600127"}',
    result_digest: '竞价量比 3.2，高开 2.1%',
    ...over,
  }
}

afterEach(cleanup)

describe('EvidenceCard · 有数据', () => {
  it('每一步都渲染：序号 / 工具名 / 耗时 / ✓', () => {
    const steps = [
      makeStep({ seq: 1, tool: 'get_auction_snapshot', elapsed_ms: 820 }),
      makeStep({ seq: 2, tool: 'get_chip_accumulation', elapsed_ms: 1500 }),
      makeStep({ seq: 3, tool: 'get_delta_series', elapsed_ms: 1880 }),
    ]
    render(<EvidenceCard run={RUN} steps={steps} />)

    expect(screen.getByTestId('evidence-card')).toBeTruthy()
    const rows = screen.getAllByTestId(/^evidence-step-\d+$/)
    expect(rows).toHaveLength(3)
    expect(rows[0].textContent).toContain('1')
    expect(rows[0].textContent).toContain('get_auction_snapshot')
    expect(rows[0].textContent).toContain('820ms')
    expect(rows[0].textContent).toContain('✓')
    expect(rows[1].textContent).toContain('get_chip_accumulation')
    expect(rows[2].textContent).toContain('1.9s')
    // 头部：问题 + 状态（中文）+ 步数/耗时
    expect(screen.getByTestId('evidence-card-question').textContent).toBe('金健米业竞价怎么看')
    expect(screen.getByTestId('evidence-card-status').textContent).toBe('成功')
    expect(screen.getByTestId('evidence-card-meta').textContent).toContain('3 步')
    expect(screen.getByTestId('evidence-card-meta').textContent).toContain('4.2s')
    // args / digest 也落到下一行
    expect(screen.getByTestId('evidence-step-args-1').textContent).toContain('600127')
    expect(screen.getByTestId('evidence-step-digest-1').textContent).toContain('竞价量比 3.2')
  })

  it('失败步骤整行标红、显示 ✗，且不被隐藏', () => {
    const steps = [
      makeStep({ seq: 1, ok: true, tool: 'get_quote' }),
      makeStep({ seq: 2, ok: false, tool: 'get_chip_accumulation', elapsed_ms: 60, result_digest: '上游超时' }),
    ]
    render(<EvidenceCard run={{ ...RUN, status: 'degraded' }} steps={steps} />)

    const failed = screen.getByTestId('evidence-step-2')
    expect(failed.getAttribute('data-ok')).toBe('false')
    expect(failed.textContent).toContain('get_chip_accumulation')
    expect(failed.textContent).toContain('✗')
    expect(failed.textContent).toContain('上游超时')
    // 整行（li 本身）带失败色，不是只有一个小图标红
    expect(failed.className).toContain('text-stock-down')
    const head = failed.firstElementChild as HTMLElement
    expect((head.firstElementChild as HTMLElement).className).toContain('text-stock-down')
    expect(failed.textContent).not.toContain('✓')
    // 成功行不受污染
    const passed = screen.getByTestId('evidence-step-1')
    expect(passed.textContent).toContain('✓')
    expect(passed.className).not.toContain('text-stock-down')
    // 降级徽标用中文，而不是把 degraded 原样丢给用户
    expect(screen.getByTestId('evidence-card-status').textContent).toBe('降级')
  })

  it('耗时缺失显示 --，不补 0ms', () => {
    const steps = [
      makeStep({ seq: 1, elapsed_ms: null }),
      makeStep({ seq: 2, tool: 'get_no_elapsed', elapsed_ms: null }),
    ]
    render(<EvidenceCard run={{ ...RUN, elapsed_ms: null, tool_calls: null }} steps={steps} />)

    const meta = screen.getByTestId('evidence-card-meta').textContent ?? ''
    expect(meta).toContain('--')
    expect(meta).not.toContain('0ms')
    const row = screen.getByTestId('evidence-step-1')
    expect(row.textContent).toContain('--')
    expect(row.textContent).not.toContain('0ms')
    expect(row.getAttribute('data-has-elapsed')).toBe('false')
  })

  it('问题超 40 字、args 超 60 字、digest 超 80 字都截断补省略号', () => {
    const question = '一'.repeat(50)
    const args = 'a'.repeat(90)
    const digest = 'b'.repeat(120)
    render(
      <EvidenceCard
        run={{ ...RUN, question }}
        steps={[makeStep({ seq: 1, args, result_digest: digest })]}
      />,
    )

    const q = screen.getByTestId('evidence-card-question').textContent ?? ''
    expect(q).toBe(`${'一'.repeat(40)}…`)
    const a = screen.getByTestId('evidence-step-args-1').textContent ?? ''
    expect(a).toBe(`${'a'.repeat(60)}…`)
    const d = screen.getByTestId('evidence-step-digest-1').textContent ?? ''
    expect(d).toBe(`${'b'.repeat(80)}…`)
  })
})

describe('EvidenceCard · 空态与异常态', () => {
  it('无 steps 时明说本轮没有工具调用', () => {
    render(<EvidenceCard run={{ ...RUN, tool_calls: 0 }} steps={[]} />)

    expect(screen.getByTestId('evidence-card-empty').textContent).toContain('本轮没有工具调用')
    expect(screen.queryByTestId('evidence-card-steps')).toBeNull()
    expect(screen.getByTestId('evidence-card-meta').textContent).toContain('0 步')
  })

  it('steps 缺失（null）时同样走空态，步数给 -- 而不是 0', () => {
    render(<EvidenceCard run={{ ...RUN, tool_calls: null }} steps={null} />)

    expect(screen.getByTestId('evidence-card-empty').textContent).toContain('本轮没有工具调用')
    expect(screen.getByTestId('evidence-card-meta').textContent).toContain('-- 步')
  })

  it('loading 态只说加载中，不预渲染假步骤', () => {
    render(<EvidenceCard run={RUN} steps={null} loading />)

    expect(screen.getByTestId('evidence-card-loading').textContent).toContain('证据加载中')
    expect(screen.queryByTestId('evidence-card-empty')).toBeNull()
    expect(screen.queryAllByTestId(/^evidence-step-\d+$/)).toHaveLength(0)
  })

  it('error 态显示失败原因，不吞掉错误', () => {
    render(<EvidenceCard run={RUN} steps={null} error="detail 接口 504" />)

    const box = screen.getByTestId('evidence-card-error')
    expect(box.textContent).toContain('证据读取失败')
    expect(box.textContent).toContain('detail 接口 504')
    expect(screen.queryByTestId('evidence-card-empty')).toBeNull()
  })

  it('run 完全缺失时不炸，问题与状态给 --', () => {
    render(<EvidenceCard run={null} steps={null} />)

    expect(screen.getByTestId('evidence-card-question').textContent).toBe('--')
    expect(screen.getByTestId('evidence-card-status').textContent).toBe('--')
    expect(screen.getByTestId('evidence-card-meta').textContent).toContain('-- 步')
    expect(screen.getByTestId('evidence-card-empty').textContent).toContain('本轮没有工具调用')
  })
})
