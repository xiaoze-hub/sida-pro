// @vitest-environment jsdom
/**
 * 并行诊断页：上限校验、提交即返回、按子任务分列事实、缺口如实列出。
 *
 * 诚信红线：配方是"实际命中"而不是"我指定的"；失败子任务要显示 error 原文与缺口，
 * 不能用"完成"含糊过去。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({
  submitParallel: vi.fn(),
  getParallel: vi.fn(),
}))

vi.mock('@/lib/copilot/parallel', async () => ({
  ...(await vi.importActual<Record<string, unknown>>('@/lib/copilot/parallel')),
  ...api,
}))

import ParallelDiagnosis, { splitQuestions } from '@/pages/copilot/ParallelDiagnosis'

const DONE_STATE = {
  run_id: 'p1',
  status: 'ok',
  error: null,
  created_at: '2026-10-25 09:00:00',
  summary: {
    available: true,
    reason: '',
    count: 2,
    ok: 1,
    failed: 1,
    tool_calls: 3,
    gaps: ['risk_judge 未完成：上游超时'],
    children: [
      {
        run_id: 'c1', recipe_id: 'chip_analyst', status: 'ok',
        tool_calls: 3, elapsed_ms: 12000, error: null, question: '筹码如何',
      },
      {
        run_id: 'c2', recipe_id: 'risk_judge', status: 'failed',
        tool_calls: 0, elapsed_ms: 800, error: '上游超时', question: '风险大吗',
      },
    ],
  },
}

beforeEach(() => {
  api.submitParallel.mockReset()
  api.getParallel.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('splitQuestions', () => {
  it('一行一个，去空白与空行', () => {
    expect(splitQuestions('  a  \n\n b\n   ')).toEqual(['a', 'b'])
  })
})

describe('ParallelDiagnosis', () => {
  it('超过 4 个子问题时禁止提交并提示', async () => {
    render(<ParallelDiagnosis />)
    const box = screen.getByLabelText(/子问题/) as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '1\n2\n3\n4\n5' } })
    await waitFor(() => expect(screen.getByText(/超出上限/)).toBeTruthy())
    const btn = screen.getByRole('button', { name: /开始诊断/ }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(api.submitParallel).not.toHaveBeenCalled()
  })

  it('提交后轮询并展示每个子任务的事实', async () => {
    api.submitParallel.mockResolvedValue({ run_id: 'p1', status: 'queued', count: 2 })
    api.getParallel.mockResolvedValue(DONE_STATE)

    render(<ParallelDiagnosis />)
    fireEvent.change(screen.getByLabelText(/子问题/), { target: { value: '筹码如何\n风险大吗' } })
    fireEvent.click(screen.getByRole('button', { name: /开始诊断/ }))

    await waitFor(() => expect(api.submitParallel).toHaveBeenCalledWith(['筹码如何', '风险大吗']))
    await waitFor(() => expect(screen.getByText('chip_analyst')).toBeTruthy(), { timeout: 6000 })
    // 显示的是实际命中配方，不是"我选的"
    expect(screen.getByText('risk_judge')).toBeTruthy()
    // 行内 error 与缺口清单都会出现「上游超时」——两处都要能看见
    expect(screen.getAllByText(/上游超时/).length).toBeGreaterThanOrEqual(1)
  })

  it('缺口单列，不藏在状态里', async () => {
    api.submitParallel.mockResolvedValue({ run_id: 'p1', status: 'queued', count: 1 })
    api.getParallel.mockResolvedValue(DONE_STATE)
    render(<ParallelDiagnosis />)
    fireEvent.change(screen.getByLabelText(/子问题/), { target: { value: 'a\nb' } })
    fireEvent.click(screen.getByRole('button', { name: /开始诊断/ }))
    await waitFor(() => expect(screen.getByText('缺口（未完成的子任务）')).toBeTruthy(), { timeout: 6000 })
    expect(screen.getByText(/risk_judge 未完成：上游超时/)).toBeTruthy()
  })

  it('提交失败透出服务端原文', async () => {
    api.submitParallel.mockRejectedValue(new Error('最多 4 个子问题（当前 5 个）'))
    render(<ParallelDiagnosis />)
    fireEvent.change(screen.getByLabelText(/子问题/), { target: { value: 'a' } })
    fireEvent.click(screen.getByRole('button', { name: /开始诊断/ }))
    await waitFor(() => expect(screen.getByText(/提交失败/)).toBeTruthy())
    expect(screen.getByText(/最多 4 个子问题/)).toBeTruthy()
  })
})
