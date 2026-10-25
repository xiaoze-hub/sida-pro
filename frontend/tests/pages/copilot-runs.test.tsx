// @vitest-environment jsdom
/**
 * 参谋任务列表页（只读）：四态分开、缺值显示 `--`、展开只拉一次、分页禁用逻辑。
 *
 * 口径红线（与页面实现一一对应）：
 *  - 「暂无运行记录」= 读到了空数组；「读取失败（可点刷新重试）」= 请求抛错。两者不许互相冒充。
 *  - 缺字段（工具数/耗时/配方/问题）一律 `--`，**不显示 0**。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({
  getCopilotRuns: vi.fn(),
  getCopilotRunDetail: vi.fn(),
}))

vi.mock('@/lib/copilot/api', async () => ({
  ...(await vi.importActual<Record<string, unknown>>('@/lib/copilot/api')),
  ...api,
}))

import CopilotRunsPage from '@/pages/copilot/RunsPage'

type RunRow = {
  run_id: string
  conversation_id: number | null
  recipe: string | null
  status: string
  tool_calls: number | null
  elapsed_ms: number | null
  tokens?: number | null
  question: string | null
  created_at: string
}

function makeRow(id: string, over: Partial<RunRow> = {}): RunRow {
  return {
    run_id: id,
    conversation_id: 1,
    recipe: 'left_diagnosis',
    status: 'ok',
    tool_calls: 3,
    elapsed_ms: 2400,
    tokens: null,
    question: `问题 ${id}`,
    created_at: '2026-10-25 09:31:12',
    ...over,
  }
}

function makeRows(n: number, prefix = 'r'): RunRow[] {
  return Array.from({ length: n }, (_, i) => makeRow(`${prefix}${i + 1}`))
}

const DETAIL = {
  run: {
    run_id: 'r1',
    status: 'ok',
    recipe: 'left_diagnosis',
    question: '问题 r1',
    tool_calls: 2,
    elapsed_ms: 2400,
    created_at: '2026-10-25 09:31:12',
  },
  steps: [
    { seq: 1, tool: 'get_auction_data', ok: true, elapsed_ms: 210, args: '{"symbol":"600127"}', result_digest: '低开' },
    { seq: 2, tool: 'get_capital_flow', ok: false, elapsed_ms: null, args: null, result_digest: null },
  ],
}

afterEach(cleanup)

beforeEach(() => {
  Object.values(api).forEach((fn) => fn.mockReset())
  api.getCopilotRuns.mockResolvedValue([makeRow('r1'), makeRow('r2', { status: 'degraded' })])
  api.getCopilotRunDetail.mockResolvedValue(DETAIL)
})

describe('copilot runs page', () => {
  it('有数据：渲染行与汇总（成功率/平均工具数）', async () => {
    render(<CopilotRunsPage />)

    const row1 = await screen.findByTestId('run-row-r1')
    expect(row1.textContent).toContain('问题 r1')
    expect(row1.textContent).toContain('左侧诊断')
    expect(screen.getByTestId('run-status-r1').textContent).toBe('成功')
    expect(screen.getByTestId('run-status-r2').textContent).toBe('降级')

    const summary = screen.getByTestId('runs-summary').textContent || ''
    expect(summary).toContain('近 2 次运行')
    expect(summary).toContain('成功率 50%')
    expect(summary).toContain('平均工具数 3.0')
    // 初始只请求第一页
    expect(api.getCopilotRuns).toHaveBeenCalledWith(20)
  })

  it('点击行展开工具步骤；再次展开命中缓存，不重复请求', async () => {
    render(<CopilotRunsPage />)
    const row = await screen.findByTestId('run-row-r1')

    fireEvent.click(row)
    expect(await screen.findByTestId('run-step-r1-1')).toBeTruthy()
    expect(screen.getByTestId('run-step-r1-1').textContent).toContain('get_auction_data')
    expect(screen.getByTestId('run-step-r1-2').textContent).toContain('失败')
    expect(api.getCopilotRunDetail).toHaveBeenCalledTimes(1)
    expect(api.getCopilotRunDetail).toHaveBeenCalledWith('r1')

    // 收起
    fireEvent.click(row)
    await waitFor(() => expect(screen.queryByTestId('run-steps-r1')).toBeNull())

    // 再展开：来自 Map 缓存，仍只有 1 次请求
    fireEvent.click(row)
    expect(await screen.findByTestId('run-step-r1-1')).toBeTruthy()
    expect(api.getCopilotRunDetail).toHaveBeenCalledTimes(1)
  })

  it('无数据：显示「暂无运行记录」而不是报错', async () => {
    api.getCopilotRuns.mockResolvedValue([])
    render(<CopilotRunsPage />)

    const empty = await screen.findByTestId('runs-empty')
    expect(empty.textContent).toContain('暂无运行记录')
    expect(screen.queryByTestId('runs-error')).toBeNull()
    expect(screen.queryByTestId('runs-loading')).toBeNull()
    const summary = screen.getByTestId('runs-summary').textContent || ''
    expect(summary).toContain('成功率 --')
    expect(summary).toContain('平均工具数 --')
  })

  it('读取失败：显示「读取失败（可点刷新重试）」并可刷新重试', async () => {
    api.getCopilotRuns.mockRejectedValueOnce(new Error('HTTP 500'))
    render(<CopilotRunsPage />)

    const err = await screen.findByTestId('runs-error')
    expect(err.textContent).toContain('读取失败（可点刷新重试）')
    expect(screen.queryByTestId('runs-empty')).toBeNull()

    // 刷新能恢复（第二次请求成功 → 有数据）
    api.getCopilotRuns.mockResolvedValueOnce([makeRow('r9')])
    fireEvent.click(screen.getByTestId('runs-refresh'))
    expect(await screen.findByTestId('run-row-r9')).toBeTruthy()
    expect(screen.queryByTestId('runs-error')).toBeNull()
  })

  it('缺字段：显示 -- 而不是 0', async () => {
    api.getCopilotRuns.mockResolvedValue([
      makeRow('rN', { tool_calls: null, elapsed_ms: null, recipe: null, question: null, created_at: '' }),
    ])
    render(<CopilotRunsPage />)

    const row = await screen.findByTestId('run-row-rN')
    const cells = Array.from(row.querySelectorAll('td')).map((td) => td.textContent || '')
    // 问题/配方/工具数/耗时/时间 五列全部缺失 → 全 `--`
    expect(cells.filter((t) => t === '--').length).toBe(5)
    // 一个数字都不许出现（0 更是禁止）
    expect(row.textContent || '').not.toMatch(/[0-9]/)

    const summary = screen.getByTestId('runs-summary').textContent || ''
    expect(summary).toContain('成功率 100%')
    // 工具数全缺 → 均值算不出，给 -- 不给 0
    expect(summary).toContain('平均工具数 --')
  })

  it('分页：首页上一页禁用；满页下一页可用；不足一页则下一页禁用', async () => {
    api.getCopilotRuns.mockResolvedValue(makeRows(20))
    render(<CopilotRunsPage />)
    await screen.findByTestId('run-row-r1')

    const prev = screen.getByTestId('runs-prev') as HTMLButtonElement
    const next = screen.getByTestId('runs-next') as HTMLButtonElement
    expect(prev.disabled).toBe(true)
    expect(next.disabled).toBe(false)
    // 本页拉满 → 后面可能还有，页数显示「至少 2 页」
    expect(screen.getByTestId('runs-table-wrap').textContent).toContain('2')

    // 翻到第 2 页：按 20*2 拉，返回 25 条 → 第 2 页只有 5 条，下一页禁用
    api.getCopilotRuns.mockResolvedValue(makeRows(25))
    fireEvent.click(next)
    await screen.findByTestId('run-row-r21')
    expect(api.getCopilotRuns).toHaveBeenLastCalledWith(40)
    expect((screen.getByTestId('runs-prev') as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getByTestId('runs-next') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByTestId('runs-table-wrap').textContent).toContain('第 2 / 2 页')
  })

  it('加载中：骨架与空态/失败态分开', async () => {
    let resolveLoad: (v: RunRow[]) => void = () => {}
    api.getCopilotRuns.mockImplementation(
      () => new Promise<RunRow[]>((res) => { resolveLoad = res }),
    )
    render(<CopilotRunsPage />)

    expect(screen.getByTestId('runs-loading')).toBeTruthy()
    expect(screen.queryByTestId('runs-empty')).toBeNull()
    expect(screen.queryByTestId('runs-error')).toBeNull()

    resolveLoad([makeRow('r1')])
    expect(await screen.findByTestId('run-row-r1')).toBeTruthy()
    expect(screen.queryByTestId('runs-loading')).toBeNull()
  })
})
