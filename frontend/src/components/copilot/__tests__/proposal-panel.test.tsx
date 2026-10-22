// @vitest-environment jsdom
/**
 * 提案面板行为回归（2026-10-22）。
 *
 * 守六件事（对应设计文档 §6 与 §10 验收线）：
 *  ① 三态：骨架 / 空态（**文案不是「0 个提案」**）/ 错误态（加载失败 + 重试，不伪装成空）；
 *  ② 待审列表**前端按 seen_count 降序**（后端给的是时间序）；
 *  ③ 采纳后出现「已采纳 · 配方仍需人工修改后才会生效」—— 产品诚信红线，缺了就是骗人；
 *  ④ 裁决走 POST {action} 到 /copilot/proposals/{id}/decide；
 *  ⑤ `seen_count` 缺失显示 `--`，不补 0；
 *  ⑥ 切 tab 带上对应 status 参数。
 *
 * 网络一律 mock（fetch 打桩），不连真后端。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { ProposalPanel } from '@/components/copilot/ProposalPanel'

interface FetchCall {
  url: string
  method: string
  body: string | null
}

const calls: FetchCall[] = []
let proposalRows: Record<string, unknown>[] = []
let summary = { total: 0, pending: 0, accept: 0, reject: 0, snooze: 0 }
let statsTop: Record<string, unknown> | null = null
/** 让 GET /copilot/proposals 首次失败（模拟接口挂掉），置 false 后恢复 */
let listFails = false
/** 裁决成功后再让列表刷新失败（模拟「裁决成功但局部刷新挂了」） */
let failListAfterDecide = false

function jsonResponse(data: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    json: async () => ({ code: ok ? 0 : 500, success: ok, data, message: ok ? 'ok' : 'err' }),
  } as unknown as Response
}

function rebuildSummary() {
  summary = { total: proposalRows.length, pending: 0, accept: 0, reject: 0, snooze: 0 }
  for (const r of proposalRows) {
    const st = String(r.status)
    if (st in summary && st !== 'total') summary[st as 'pending'] += 1
  }
}

beforeEach(() => {
  calls.length = 0
  listFails = false
  failListAfterDecide = false
  proposalRows = [
    // 后端按 last_seen_at 降序返回：id=1 最新但只提过 1 次
    {
      id: 1,
      dimension: '北向资金日内节奏',
      reason: '配方只有日频净流入，看不到日内节奏',
      evidence: '',
      symbol: '600000',
      recipe: 'capital_flow',
      status: 'pending',
      seen_count: 1,
      first_seen_at: '2026-10-21 10:00:00',
      last_seen_at: '2026-10-22 10:00:00',
    },
    {
      id: 2,
      dimension: '股东户数变化',
      reason: '配方只覆盖十大股东，看不到户数变化趋势',
      evidence: 'Q: 这只票股东户数最近有变化吗？\nA: 平台没有户数变化口径。',
      symbol: '600111',
      recipe: 'demon_diagnosis',
      status: 'pending',
      seen_count: 5,
      first_seen_at: '2026-10-20 09:00:00',
      last_seen_at: '2026-10-21 09:00:00',
      // P2: 注册表里早就有工具覆盖这个维度（只是没进配方）
      candidate_tools: [
        { name: 'get_fundamentals_detail', caliber: '龙虎榜/两融/股东户数/分红/事件(公开数据聚合)' },
      ],
    },
    {
      id: 3,
      dimension: '龙虎榜席位风格',
      reason: '席位标签有，但风格画像缺失',
      evidence: '',
      symbol: null,
      recipe: null,
      status: 'accept',
      seen_count: 2,
      first_seen_at: '2026-10-19 09:00:00',
      last_seen_at: '2026-10-20 09:00:00',
    },
  ]
  rebuildSummary()
  statsTop = { dimension: '股东户数变化', seen_count: 5, status: 'pending', rows: 1 }

  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const href = String(url)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ url: href, method, body: typeof init?.body === 'string' ? init.body : null })

      // 裁决（POST 必须在列表分支之前判，否则被 /proposals 前缀吃掉）
      const decideMatch = /\/copilot\/proposals\/(\d+)\/decide/.exec(href)
      if (method === 'POST' && decideMatch) {
        const id = Number(decideMatch[1])
        const action = JSON.parse(String(init?.body || '{}')).action as string
        const row = proposalRows.find((r) => r.id === id)
        if (!row) return jsonResponse(null, false)
        row.status = action
        rebuildSummary()
        if (failListAfterDecide) listFails = true
        return jsonResponse({ ok: true, proposal: { ...row } })
      }

      if (href.includes('/copilot/proposals/stats')) {
        return jsonResponse({ items: statsTop ? [statsTop] : [], top: statsTop, summary })
      }

      if (href.includes('/copilot/proposals')) {
        if (listFails) return jsonResponse(null, false)
        const status = /[?&]status=([^&]+)/.exec(href)?.[1]
        const items = status ? proposalRows.filter((r) => r.status === status) : proposalRows
        return jsonResponse(items)
      }

      return jsonResponse([])
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('ProposalPanel', () => {
  it('渲染汇总带与列表：次数 / 状态徽标 / 理由 / 元信息', async () => {
    render(<ProposalPanel />)
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))

    // 汇总带四个数（待审 2 / 采纳 1 / 忽略 0 / 搁置 0）
    expect(screen.getByTestId('proposal-count-pending').textContent).toBe('2')
    expect(screen.getByTestId('proposal-count-accept').textContent).toBe('1')
    expect(screen.getByTestId('proposal-count-reject').textContent).toBe('0')
    // 最该补：维度名 + 次数
    expect(screen.getByTestId('proposal-top').textContent).toContain('股东户数变化')
    expect(screen.getByTestId('proposal-top').textContent).toContain('×5')

    // 行内容（维度名在「最该补」与列表行各出现一次）
    expect(screen.getAllByText('股东户数变化')).toHaveLength(2)
    expect(screen.getAllByTestId('proposal-dimension').map((el) => el.textContent)).toContain('股东户数变化')
    expect(screen.getByText('配方只覆盖十大股东，看不到户数变化趋势')).toBeTruthy()
    expect(screen.getByText('600111 · demon_diagnosis · 10-20 起')).toBeTruthy()
    // 元信息缺项整段省略（id=1 没有首现问题，id=3 不在待审）
    const seen = screen.getAllByTestId('proposal-seen').map((el) => el.textContent)
    expect(seen).toEqual(['×5', '×1'])
    const badges = screen.getAllByTestId('proposal-status').map((el) => el.textContent)
    expect(badges).toEqual(['待审', '待审'])
  })

  it('已有工具覆盖时显示「可挂载现有工具」；没有候选的行不显示这一句', async () => {
    render(<ProposalPanel />)
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))

    // 只有 id=2（股东户数变化）带 candidate_tools → 只出现一行提示
    const lines = screen.getAllByTestId('proposal-candidate-tools')
    expect(lines).toHaveLength(1)
    expect(lines[0].textContent).toBe('可挂载现有工具：get_fundamentals_detail')
    // 后端给的顺序原样显示，不在前端重排/猜测口径
    expect(lines[0].getAttribute('title')).toBe('可挂载现有工具：get_fundamentals_detail')
  })

  it('待审列表按 seen_count 降序（后端给的是时间序）', async () => {
    render(<ProposalPanel />)
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))

    // 后端顺序是 id=1（最新）在前；面板必须把 ×5 提到了前面
    const dims = screen.getAllByTestId('proposal-dimension').map((el) => el.textContent)
    expect(dims).toEqual(['股东户数变化', '北向资金日内节奏'])
    const ids = screen.getAllByTestId('proposal-row').map((el) => el.getAttribute('data-proposal-id'))
    expect(ids).toEqual(['2', '1'])
  })

  it('采纳：POST {action:"accept"} + 出现「配方仍需人工修改」提示 + 该行离开待审', async () => {
    render(<ProposalPanel />)
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))

    const acceptBtn = screen
      .getAllByTestId('proposal-action-accept')
      .find((el) => el.getAttribute('data-proposal-id') === '2')
    expect(acceptBtn).toBeTruthy()
    fireEvent.click(acceptBtn as HTMLElement)

    await waitFor(() => {
      const post = calls.find((c) => c.method === 'POST')
      expect(post).toBeTruthy()
      expect(post?.url).toContain('/api/copilot/proposals/2/decide')
      expect(JSON.parse(String(post?.body))).toEqual({ action: 'accept' })
    })

    // 诚信红线：不弹 toast 不弹窗，就一行 muted 文案
    const note = await screen.findByTestId('proposal-accept-note')
    expect(note.textContent).toBe('已采纳 · 配方仍需人工修改后才会生效')

    // 终态不再出现在「待审」（「最该补」那行仍会显示它 —— 那是聚合口径，不是列表行）
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(1))
    expect(screen.getAllByTestId('proposal-dimension').map((el) => el.textContent)).not.toContain('股东户数变化')
  })

  it('忽略：不出现「已采纳」提示（那句只对采纳成立）', async () => {
    render(<ProposalPanel />)
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))

    const rejectBtn = screen
      .getAllByTestId('proposal-action-reject')
      .find((el) => el.getAttribute('data-proposal-id') === '1')
    fireEvent.click(rejectBtn as HTMLElement)

    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    expect(JSON.parse(String(calls.find((c) => c.method === 'POST')?.body))).toEqual({ action: 'reject' })
    expect(screen.queryByTestId('proposal-accept-note')).toBeNull()
  })

  it('采纳成功但刷新失败：列表保留（不抹空），只补一行刷新失败提示', async () => {
    failListAfterDecide = true
    render(<ProposalPanel />)
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))

    const acceptBtn = screen
      .getAllByTestId('proposal-action-accept')
      .find((el) => el.getAttribute('data-proposal-id') === '2')
    fireEvent.click(acceptBtn as HTMLElement)

    expect((await screen.findByTestId('proposal-accept-note')).textContent).toBe(
      '已采纳 · 配方仍需人工修改后才会生效',
    )
    const refreshErr = await screen.findByTestId('proposal-refresh-error')
    expect(refreshErr.textContent).toContain('刷新失败')
    // 裁决已经成功：不能因为刷新挂了就把列表抹成错误态/空态
    expect(screen.getAllByTestId('proposal-row')).toHaveLength(2)
    expect(screen.queryByTestId('proposal-error')).toBeNull()
    expect(screen.queryByTestId('proposal-empty')).toBeNull()
  })

  it('空态：显示「还没有提案」，绝不显示「0 个提案」', async () => {
    proposalRows = []
    rebuildSummary()
    statsTop = null
    render(<ProposalPanel />)

    const empty = await screen.findByTestId('proposal-empty')
    expect(empty.textContent).toContain('还没有提案')
    expect(empty.textContent).toContain('参谋会记在这里')
    expect(document.body.textContent).not.toContain('0 个提案')
  })

  it('空态（某分类无记录）：不说「还没有提案」，只说这个分类没有', async () => {
    proposalRows = proposalRows.filter((r) => r.status === 'pending')
    rebuildSummary()
    render(<ProposalPanel />)
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))

    fireEvent.click(screen.getByTestId('proposal-tab-reject'))
    const tabEmpty = await screen.findByTestId('proposal-empty-tab')
    expect(tabEmpty.textContent).toBe('这个分类下没有提案')
    expect(screen.queryByTestId('proposal-empty')).toBeNull()
  })

  it('错误态：加载失败 + 重试，不伪装成空态；重试成功后出列表', async () => {
    listFails = true
    render(<ProposalPanel />)

    const err = await screen.findByTestId('proposal-error')
    expect(err.textContent).toContain('加载失败')
    // 挂了 ≠ 没有：空态文案一个都不许出现
    expect(screen.queryByTestId('proposal-empty')).toBeNull()
    expect(screen.queryByTestId('proposal-empty-tab')).toBeNull()

    listFails = false
    fireEvent.click(screen.getByTestId('proposal-retry'))
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))
    expect(screen.queryByTestId('proposal-error')).toBeNull()
  })

  it('切 tab：带上对应 status 参数，全部 tab 不带 status', async () => {
    render(<ProposalPanel />)
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))

    fireEvent.click(screen.getByTestId('proposal-tab-accept'))
    await waitFor(() => {
      expect(calls.some((c) => c.url.includes('/copilot/proposals?limit=50&status=accept'))).toBe(true)
    })
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(1))
    expect(screen.getByText('龙虎榜席位风格')).toBeTruthy()

    fireEvent.click(screen.getByTestId('proposal-tab-all'))
    await waitFor(() => {
      const listCall = calls.filter((c) => c.url.includes('/copilot/proposals?limit=50')).pop()
      expect(listCall?.url).not.toContain('status=')
    })
    // 全部 tab：保持后端时间序（不再按次数重排）
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(3))
    const ids = screen.getAllByTestId('proposal-row').map((el) => el.getAttribute('data-proposal-id'))
    expect(ids).toEqual(['1', '2', '3'])
  })

  it('seen_count 缺失显示 --，不补 0', async () => {
    proposalRows = [
      {
        id: 7,
        dimension: '筹码集中度分位',
        reason: '没有分位口径',
        evidence: '',
        symbol: null,
        recipe: null,
        status: 'pending',
        seen_count: null,
        first_seen_at: '',
        last_seen_at: '',
      },
    ]
    rebuildSummary()
    statsTop = null
    render(<ProposalPanel />)

    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(1))
    expect(screen.getByTestId('proposal-seen').textContent).toBe('--')
    expect(screen.getByTestId('proposal-seen').textContent).not.toBe('×0')
    // 元信息三项全缺 → 整行不渲染
    expect(screen.queryByTestId('proposal-meta')).toBeNull()
  })

  it('点行展开证据，再点收起', async () => {
    render(<ProposalPanel />)
    await waitFor(() => expect(screen.getAllByTestId('proposal-row')).toHaveLength(2))

    const expand = screen
      .getAllByTestId('proposal-expand')
      .find((el) => el.closest('[data-proposal-id]')?.getAttribute('data-proposal-id') === '2')
    fireEvent.click(expand as HTMLElement)

    const evidence = await screen.findByTestId('proposal-evidence')
    expect(evidence.textContent).toContain('股东户数最近有变化吗')
    expect(evidence.className).toContain('whitespace-pre-wrap')

    fireEvent.click(expand as HTMLElement)
    await waitFor(() => expect(screen.queryByTestId('proposal-evidence')).toBeNull())
  })
})
