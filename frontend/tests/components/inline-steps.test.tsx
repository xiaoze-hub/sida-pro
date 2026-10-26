// @vitest-environment jsdom
import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import InlineSteps from '@/components/copilot/InlineSteps'
import type { CopilotRunRow } from '@/lib/copilot/api'

afterEach(() => cleanup())

const run: CopilotRunRow = {
  run_id: 'r1',
  conversation_id: 7,
  recipe: 'market_attribution',
  status: 'ok',
  tool_calls: 7,
  elapsed_ms: 12400,
  question: '上证7月到10月为什么跌',
  created_at: '2026-10-25T09:00:00',
}

const steps = [
  { seq: 1, tool: 'get_thsdk_market_data_index', ok: true, elapsed_ms: 320, args: '{"code":"000001"}', result_digest: '上证 3690.48 +0.89%' },
  { seq: 2, tool: 'get_range_summary', ok: true, elapsed_ms: 810, args: '{"symbol":"000001","start_date":"2026-07-01"}', result_digest: '-12.07%' },
  { seq: 3, tool: 'get_margin_trading', ok: false, elapsed_ms: 5000, args: null, result_digest: null },
]

describe('InlineSteps（答案下面的步骤条）', () => {
  it('默认收起：只有一行摘要，不占版面', () => {
    render(<InlineSteps run={run} />)
    expect(screen.getByTestId('inline-steps-toggle').textContent).toContain('过程')
    expect(screen.getByTestId('inline-steps-toggle').textContent).toContain('7 步')
    expect(screen.getByTestId('inline-steps-toggle').textContent).toContain('12.4s')
    expect(screen.queryByTestId('inline-steps-list')).toBeNull()
  })

  it('展开才拉详情（打开会话不该为每轮都打请求）', () => {
    const onLoad = vi.fn()
    render(<InlineSteps run={run} onLoad={onLoad} />)
    expect(onLoad).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    expect(onLoad).toHaveBeenCalledTimes(1)
  })

  it('已加载过详情时再展开不重复请求', () => {
    const onLoad = vi.fn()
    render(<InlineSteps run={run} steps={steps} onLoad={onLoad} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    expect(onLoad).not.toHaveBeenCalled()
    expect(screen.getAllByTestId('inline-step')).toHaveLength(3)
  })

  it('展开后每步给工具名 / 参数 / 耗时 / 成功标记', () => {
    render(<InlineSteps run={run} steps={steps} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    const list = screen.getByTestId('inline-steps-list')
    expect(list.textContent).toContain('get_range_summary')
    expect(list.textContent).toContain('000001')
    expect(list.textContent).toContain('810ms')
    expect(list.textContent).toContain('✓')
  })

  it('失败步骤显式标红 ✗，不隐藏', () => {
    render(<InlineSteps run={run} steps={steps} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    const rows = screen.getAllByTestId('inline-step')
    const last = rows[rows.length - 1]
    expect(last.textContent).toContain('✗')
    expect(last.textContent).toContain('5.0s')
  })

  it('耗时缺失给 -- 不补 0', () => {
    render(<InlineSteps run={{ ...run, elapsed_ms: null, tool_calls: 0 }} />)
    const t = screen.getByTestId('inline-steps-toggle').textContent || ''
    expect(t).toContain('--')
    expect(t).not.toContain('0ms')
  })

  it('非 ok 状态在摘要里标出来', () => {
    render(<InlineSteps run={{ ...run, status: 'degraded' }} />)
    expect(screen.getByTestId('inline-steps-toggle').textContent).toContain('degraded')
  })

  it('详情拿不到时给一句静默提示，不炸页面', () => {
    render(<InlineSteps run={run} failed />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    expect(screen.getByTestId('inline-steps-list').textContent).toContain('没取到')
  })

  it('空步骤说"本轮没有工具调用"，不显示 0', () => {
    render(<InlineSteps run={run} steps={[]} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    expect(screen.getByTestId('inline-steps-list').textContent).toContain('没有工具调用')
  })

  it('参数过长截断（列表要一眼扫完）', () => {
    const longArg = '{"symbol":"000001","note":"' + 'x'.repeat(200) + '"}'
    render(<InlineSteps run={run} steps={[{ ...steps[0], args: longArg }]} />)
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    const row = screen.getByTestId('inline-step')
    expect(row.textContent).toContain('…')
    expect(row.textContent!.length).toBeLessThan(120)
  })
})
