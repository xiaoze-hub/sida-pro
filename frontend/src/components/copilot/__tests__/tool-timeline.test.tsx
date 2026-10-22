// @vitest-environment jsdom
/**
 * ToolTimeline 回归（2026-10-22）。
 *
 * 覆盖四件事：
 *  1. 三轮都渲染（时间 / 配方 / 工具数 / 耗时 / 状态徽标）；
 *  2. 点开某轮触发 onLoadDetail（纯展示组件，不自己发请求）；
 *  3. steps 渲染工具名与失败原因；
 *  4. 空态文案「本轮没有工具调用」。
 *
 * 口径：缺失一律 `--`，不补 0；状态色用语义色（灰/橙/红），不用渐变。
 * 断言风格：只用 vitest 原生 matcher，不引 jest-dom（tsc -b 会扫 src）。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ToolTimeline } from '@/components/copilot/ToolTimeline'
import type { CopilotRunRow } from '@/lib/copilot/api'

function makeRun(over: Partial<CopilotRunRow> = {}): CopilotRunRow {
  return {
    run_id: 'r1',
    conversation_id: 1,
    recipe: null,
    status: 'ok',
    tool_calls: 2,
    elapsed_ms: 3200,
    tokens: null,
    question: '金健米业竞价怎么样',
    created_at: '2026-10-20 15:00:00',
    ...over,
  }
}

const RUNS: CopilotRunRow[] = [
  makeRun({ run_id: 'r1', recipe: null, status: 'ok', tool_calls: 2, elapsed_ms: 3200 }),
  makeRun({
    run_id: 'r2',
    recipe: 'auction_strong',
    status: 'degraded',
    tool_calls: 5,
    elapsed_ms: 850,
    created_at: '2026-10-20 15:02:00',
  }),
  makeRun({
    run_id: 'r3',
    recipe: 'full_dossier',
    status: 'error',
    tool_calls: 0,
    elapsed_ms: null,
    created_at: '2026-10-20 15:05:00',
  }),
]

afterEach(cleanup)

describe('ToolTimeline 骨架', () => {
  it('三轮都渲染：配方名（无则「常规」）、工具数、耗时、状态徽标', () => {
    render(<ToolTimeline runs={RUNS} />)

    expect(screen.getByTestId('copilot-run-row-r1')).toBeTruthy()
    expect(screen.getByTestId('copilot-run-row-r2')).toBeTruthy()
    expect(screen.getByTestId('copilot-run-row-r3')).toBeTruthy()

    // 无配方 → 「常规」
    expect(screen.getByTestId('copilot-run-row-r1').textContent).toContain('常规')
    expect(screen.getByTestId('copilot-run-row-r2').textContent).toContain('auction_strong')

    // 工具数 / 耗时单位自动（ms → s）
    expect(screen.getByTestId('copilot-run-row-r1').textContent).toContain('2 工具')
    expect(screen.getByTestId('copilot-run-row-r1').textContent).toContain('3.2s')
    expect(screen.getByTestId('copilot-run-row-r2').textContent).toContain('850ms')
    // 耗时缺失 → `--`，不补 0
    expect(screen.getByTestId('copilot-run-row-r3').textContent).toContain('--')

    // 状态徽标：ok 灰 / degraded 橙 / error 红（语义色，不是渐变）
    expect(screen.getByTestId('copilot-run-status-r1').getAttribute('class')).toContain(
      'text-muted-foreground',
    )
    expect(screen.getByTestId('copilot-run-status-r2').getAttribute('class')).toContain(
      'text-amber-500',
    )
    expect(screen.getByTestId('copilot-run-status-r3').getAttribute('class')).toContain(
      'text-red-500',
    )
  })
})

describe('ToolTimeline 展开', () => {
  it('点开某轮触发 onLoadDetail（组件自己不取数）', () => {
    const onLoadDetail = vi.fn()
    render(<ToolTimeline runs={RUNS} onLoadDetail={onLoadDetail} />)

    expect(onLoadDetail).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('copilot-run-row-r2'))
    expect(onLoadDetail).toHaveBeenCalledTimes(1)
    expect(onLoadDetail).toHaveBeenCalledWith('r2')

    // 收起不重复请求
    fireEvent.click(screen.getByTestId('copilot-run-row-r2'))
    expect(onLoadDetail).toHaveBeenCalledTimes(1)
  })

  it('详情未回来时显示「加载中」骨架', () => {
    render(<ToolTimeline runs={RUNS} onLoadDetail={vi.fn()} />)
    fireEvent.click(screen.getByTestId('copilot-run-row-r2'))
    expect(screen.getByTestId('copilot-run-loading')).toBeTruthy()
    expect(screen.getByTestId('copilot-run-loading').textContent).toContain('加载中')
  })

  it('steps 渲染工具名与失败原因', () => {
    render(
      <ToolTimeline
        runs={RUNS}
        details={{
          r2: {
            run: {
              run_id: 'r2',
              status: 'degraded',
              recipe: 'auction_strong',
              question: '金健米业竞价怎么样',
              tool_calls: 2,
              elapsed_ms: 850,
              created_at: '2026-10-20 15:02:00',
            },
            steps: [
              {
                seq: 1,
                tool: 'quote_snapshot',
                ok: true,
                elapsed_ms: 120,
                args: '{"symbol":"600127"}',
                result_digest: '收盘 12.30，量比 1.8',
              },
              {
                seq: 2,
                tool: 'auction_flow',
                ok: false,
                elapsed_ms: 730,
                args: null,
                result_digest: null,
                error: '竞价数据源超时',
              },
            ],
          },
        }}
      />,
    )
    fireEvent.click(screen.getByTestId('copilot-run-row-r2'))

    // 工具名
    expect(screen.getByTestId('copilot-step-1').textContent).toContain('quote_snapshot')
    expect(screen.getByTestId('copilot-step-2').textContent).toContain('auction_flow')
    // 失败原因
    expect(screen.getByTestId('copilot-step-2').textContent).toContain('失败')
    expect(screen.getByTestId('copilot-step-2').textContent).toContain('竞价数据源超时')
    // 成功步给摘要 + ok
    expect(screen.getByTestId('copilot-step-1').textContent).toContain('ok')
    expect(screen.getByTestId('copilot-step-1').textContent).toContain('量比 1.8')
  })
})

describe('ToolTimeline 空态', () => {
  it('runs 为空 → 「本轮没有工具调用」', () => {
    render(<ToolTimeline runs={[]} />)
    expect(screen.getByTestId('copilot-tool-timeline-empty').textContent).toContain(
      '本轮没有工具调用',
    )
  })

  it('该轮工具数为 0 → 展开后仍是「本轮没有工具调用」（不是"加载中"）', () => {
    render(<ToolTimeline runs={[makeRun({ run_id: 'r3', tool_calls: 0 })]} />)
    fireEvent.click(screen.getByTestId('copilot-run-row-r3'))
    expect(screen.getByTestId('copilot-run-empty').textContent).toContain('本轮没有工具调用')
    expect(screen.queryByTestId('copilot-run-loading')).toBeNull()
  })

  it('详情已回且 steps 为空 → 「本轮没有工具调用」', () => {
    render(
      <ToolTimeline
        runs={[makeRun({ run_id: 'r1', tool_calls: 2 })]}
        details={{ r1: { steps: [] } }}
      />,
    )
    fireEvent.click(screen.getByTestId('copilot-run-row-r1'))
    expect(screen.getByTestId('copilot-run-empty').textContent).toContain('本轮没有工具调用')
  })
})
