// @vitest-environment jsdom
/**
 * SchedulePanel 行为回归（2026-10-22）。
 *
 * 守四件事：
 *  ① enabled / disabled 两种行都能渲染并**可分辨**；
 *  ② 新建调 POST，且时间必须是 HH:MM（格式不对就不许发请求）；
 *  ③ 启停调 PATCH；
 *  ④ `last_run_date` 为空时显示 `--`，**不许显示「今天」**这类编造口径。
 *
 * 网络一律 mock（`fetch` 打桩），不连真后端。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { SchedulePanel, deriveNextRun, formatLastRun } from '@/components/copilot/SchedulePanel'

interface FetchCall {
  url: string
  method: string
  body: string | null
}

const calls: FetchCall[] = []
let scheduleRows: unknown[] = []

function jsonResponse(data: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    json: async () => ({ code: ok ? 0 : 500, success: ok, data, message: ok ? 'ok' : 'err' }),
  } as unknown as Response
}

beforeEach(() => {
  calls.length = 0
  scheduleRows = [
    {
      id: 1,
      kind: 'auction_digest',
      at_hhmm: '09:20',
      weekdays: 'mon-fri',
      conversation_id: null,
      enabled: true,
      last_run_date: '20261021',
      created_at: '2026-10-21 08:00:00',
    },
    {
      id: 2,
      kind: 'daily_brief',
      at_hhmm: '15:30',
      weekdays: 'mon-fri',
      conversation_id: null,
      enabled: false,
      last_run_date: null,
      created_at: '2026-10-21 08:00:00',
    },
  ]
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    calls.push({
      url: String(url),
      method,
      body: typeof init?.body === 'string' ? init.body : null,
    })
    if (method === 'GET') return jsonResponse(scheduleRows)
    if (method === 'POST') {
      const parsed = JSON.parse(String(init?.body || '{}'))
      const created = {
        id: 88,
        kind: parsed.kind,
        at_hhmm: parsed.at_hhmm,
        weekdays: parsed.weekdays,
        conversation_id: null,
        enabled: true,
        last_run_date: null,
        created_at: '2026-10-22 09:00:00',
      }
      scheduleRows = [created, ...scheduleRows]
      return jsonResponse(created)
    }
    return jsonResponse({ ok: true })
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('SchedulePanel', () => {
  it('渲染 enabled 与 disabled 两种任务，可分辨', async () => {
    render(<SchedulePanel />)
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(2))

    const rows = screen.getAllByTestId('schedule-row')
    // 行内标题（下拉里也有同名 option，故按行取）
    expect(rows[0].textContent).toContain('竞价摘要')
    expect(rows[1].textContent).toContain('收盘小结')
    expect(screen.getAllByTestId('schedule-time')[0].textContent).toContain('09:20')

    const toggles = screen.getAllByTestId('schedule-toggle')
    expect(toggles[0].getAttribute('data-state')).toBe('checked')
    expect(toggles[1].getAttribute('data-state')).toBe('unchecked')
    // 停用行有显式标记，不靠开关自己说
    expect(screen.getByText('已停用')).toBeTruthy()
  })

  it('空态文案固定', async () => {
    scheduleRows = []
    render(<SchedulePanel />)
    const empty = await screen.findByTestId('schedule-empty')
    expect(empty.textContent).toContain('还没有定时任务')
    expect(empty.textContent).toContain('9:20')
  })

  it('新建：调 POST 且带 kind / at_hhmm / weekdays', async () => {
    render(<SchedulePanel />)
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(2))

    fireEvent.change(screen.getByTestId('schedule-time-input'), { target: { value: '09:20' } })
    fireEvent.click(screen.getByTestId('schedule-create-btn'))

    await waitFor(() => {
      const post = calls.find((c) => c.method === 'POST')
      expect(post).toBeTruthy()
      expect(post?.url).toContain('/api/copilot/schedules')
      const body = JSON.parse(String(post?.body))
      expect(body.kind).toBe('auction_digest')
      expect(body.at_hhmm).toBe('09:20')
      expect(typeof body.weekdays).toBe('string')
    })
  })

  it('新建：时间不是 HH:MM 时不发请求', async () => {
    render(<SchedulePanel />)
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(2))

    const input = screen.getByTestId('schedule-time-input') as HTMLInputElement
    // jsdom 的 type=time 会把非法值清空 —— 组件必须把空值当"没填"，不许发请求。
    fireEvent.change(input, { target: { value: '25:99' } })
    expect(input.value).toBe('')

    const btn = screen.getByTestId('schedule-create-btn') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(calls.find((c) => c.method === 'POST')).toBeUndefined()
  })

  it('工作日多选：可切换，按钮带 aria-pressed', async () => {
    render(<SchedulePanel />)
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(2))

    const sat = screen.getByTestId('schedule-weekday-sat')
    expect(sat.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(sat)
    expect(screen.getByTestId('schedule-weekday-sat').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('schedule-weekday-mon').getAttribute('aria-pressed')).toBe('true')
  })

  it('开关：调 PATCH 且带上 enabled', async () => {
    render(<SchedulePanel />)
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(2))

    fireEvent.click(screen.getAllByTestId('schedule-toggle')[1])

    await waitFor(() => {
      const patch = calls.find((c) => c.method === 'PATCH')
      expect(patch).toBeTruthy()
      expect(patch?.url).toContain('/api/copilot/schedules/2')
      expect(JSON.parse(String(patch?.body)).enabled).toBe(true)
    })
  })

  it('last_run_date 为空：显示 --，不显示「今天」', async () => {
    render(<SchedulePanel />)
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(2))

    const lastRun = screen.getAllByTestId('schedule-last-run')
    // 第一行有值 → 格式化后的日期
    expect(lastRun[0].textContent).toContain('2026-10-21')
    // 第二行没跑过 → --
    expect(lastRun[1].textContent).toContain('--')

    const text = document.body.textContent || ''
    expect(text).not.toContain('今天')
    expect(text).not.toContain('今天已跑')
  })

  it('last_run_date 有值时可推出「下次」，没有则 --', async () => {
    render(<SchedulePanel />)
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(2))

    const nextRuns = screen.getAllByTestId('schedule-next-run')
    // 第一行有锚点 → 给出推算日期
    expect(nextRuns[0].textContent).not.toContain('--')
    // 第二行无锚点 → 推不出来就 --
    expect(nextRuns[1].textContent).toContain('--')
  })

  it('删除：二次确认后才调 DELETE', async () => {
    render(<SchedulePanel />)
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(2))

    fireEvent.click(screen.getAllByTestId('schedule-delete')[0])
    expect(screen.getByTestId('schedule-delete-confirm')).toBeTruthy()
    expect(calls.find((c) => c.method === 'DELETE')).toBeUndefined()

    fireEvent.click(screen.getByTestId('schedule-delete-confirm'))
    await waitFor(() => {
      const del = calls.find((c) => c.method === 'DELETE')
      expect(del).toBeTruthy()
      expect(del?.url).toContain('/api/copilot/schedules/1')
    })
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(1))
  })

  it('合规：不出现单点买卖指令类措辞', async () => {
    render(<SchedulePanel />)
    await waitFor(() => expect(screen.getAllByTestId('schedule-row')).toHaveLength(2))
    const text = document.body.textContent || ''
    for (const w of ['建议买入', '建议卖出', '主力建仓', '主力出货']) {
      expect(text).not.toContain(w)
    }
  })
})

describe('SchedulePanel 纯函数', () => {
  it('formatLastRun：空 → --，YYYYMMDD → YYYY-MM-DD', () => {
    expect(formatLastRun(null)).toBe('--')
    expect(formatLastRun('')).toBe('--')
    expect(formatLastRun('20261021')).toBe('2026-10-21')
  })

  it('deriveNextRun：无锚点 → null（不许拿今天硬推）', () => {
    const now = new Date(2026, 9, 22, 8, 0)
    expect(deriveNextRun(null, '09:20', 'mon-fri', now)).toBeNull()
    expect(deriveNextRun('not-a-date', '09:20', 'mon-fri', now)).toBeNull()
  })

  it('deriveNextRun：有锚点 → 按工作日推进到下一个匹配日', () => {
    // 2026-10-21 是周三；锚点次日=周四，工作日 mon-fri → 周四
    const now = new Date(2026, 9, 22, 8, 0)
    expect(deriveNextRun('20261021', '09:20', 'mon-fri', now)).toBe('10月22日 09:20')
    // 只选周一 → 从周三往后推到下一个周一 = 10月26日
    expect(deriveNextRun('20261021', '09:20', 'mon', now)).toBe('10月26日 09:20')
    // 时间不合法 → 推不出来
    expect(deriveNextRun('20261021', '99:99', 'mon-fri', now)).toBeNull()
    // 工作日集合认不出 → 推不出来
    expect(deriveNextRun('20261021', '09:20', 'xyz', now)).toBeNull()
  })

  it('deriveNextRun：锚点很久没跑过时，不许推出**过去**的日期', () => {
    // 锚点停在 10 月 5 日(now=10/22)，若只从锚点次日推会算出 10 月 6 日(历史) —— 那是撒谎。
    const now = new Date(2026, 9, 22, 8, 0)
    const next = deriveNextRun('20261005', '09:20', 'mon-fri', now)
    expect(next).not.toBeNull()
    expect(next).toBe('10月22日 09:20')
  })

  it('deriveNextRun：当天时间已过 → 顺延到下一个匹配工作日', () => {
    // now = 周四 10:00，锚点=当天(已跑)，时间 09:20 已过 → 应推到周五
    const now = new Date(2026, 9, 22, 10, 0)
    expect(deriveNextRun('20261022', '09:20', 'mon-fri', now)).toBe('10月23日 09:20')
    // 当天时间还没到 → 就是当天
    const morning = new Date(2026, 9, 22, 8, 0)
    expect(deriveNextRun('20261021', '09:20', 'mon-fri', morning)).toBe('10月22日 09:20')
  })
})
