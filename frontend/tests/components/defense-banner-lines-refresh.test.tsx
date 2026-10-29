// @vitest-environment jsdom
/**
 * B5（2026-10-29 独立复审）· 防线持仓只在挂载时取一次 ⇒ 别处改了/删了防线，告警条还照着
 * 旧防线报警。
 *
 * 复现：工作台开着某只票（告警条已挂载），用户在持仓页（或另一个浏览器标签页 / 助手）
 *   ① 把防线从 34 改到 30 —— 告警条仍拿 34 判"已击穿"；
 *   ② 把这条持仓删掉 —— 告警条仍显示"已击穿你的防守线"；
 *   ③ 或者刷新失败 —— 告警条把最后一次成功读到的防线当成当前事实继续报警。
 *
 * 契约：
 *   - 任何地方保存/删除防线后，通过 `notifyDefenseLinesChanged()`（同时广播 window 事件，
 *     跨组件/跨标签页）让告警条**立刻**重取持仓行；
 *   - 告警条自己也按 `DEFENSE_QUOTE_REFRESH_MS` 续期持仓行（无事件时的兜底，覆盖助手/脚本改动）；
 *   - 续期失败 ⇒ 不再拿旧防线出破位结论，降级为「时效未核实」；
 *   - 持仓行已不存在 ⇒ 明说"不在持仓中"，不继续报警。
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({ fetchAPI: api.fetchAPI }))

import { DEFENSE_QUOTE_REFRESH_MS, DefenseLineBanner } from '@/components/DefenseLineBanner'
import {
  DEFENSE_LINES_CHANGED_EVENT,
  DEFENSE_LINES_STORAGE_KEY,
  notifyDefenseLinesChanged,
} from '@/lib/defense-lines'

const MARKETS = [{ code: 'CN', name: 'A股', status: 'trading', local_time: '09:31' }]
const QUOTE = { current_price: 33, quote_time: '2026-10-29 09:31:00', daily_pnl_period: 'today' }

const withDefense = (defense: number | null) => ({
  accounts: [
    {
      id: 1,
      name: '主账户',
      positions: [{ id: 11, symbol: '600111', market: 'CN', cost_price: 36.52, defense_price: defense }],
    },
  ],
})

const NO_POSITION = { accounts: [{ id: 1, name: '主账户', positions: [] }] }

async function flush(rounds = 14) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}

const summaryCalls = () =>
  api.fetchAPI.mock.calls.filter((c) => String(c[0]).includes('/portfolio/summary')).length

function mockApi(opts: { summary: () => unknown; quote?: () => unknown }) {
  api.fetchAPI.mockImplementation((url: string, init?: { body?: string }) => {
    const u = String(url)
    if (u.includes('/portfolio/summary')) {
      const v = opts.summary()
      return v instanceof Error ? Promise.reject(v) : Promise.resolve(v)
    }
    if (u.includes('/stocks/markets/status')) return Promise.resolve(MARKETS)
    if (u.includes('/quotes/')) {
      const payload = opts.quote ? opts.quote() : QUOTE
      return payload instanceof Error ? Promise.reject(payload) : Promise.resolve(payload)
    }
    if (u.includes('/positions/')) {
      // 保存防线（PUT /positions/{id}/defense）：回显提交的绝对值
      const body = JSON.parse(String(init?.body ?? '{}')) as { defense_price?: number | null }
      return Promise.resolve({ defense_price: body.defense_price ?? null })
    }
    return Promise.reject(new Error(`unexpected url ${url}`))
  })
}

beforeEach(() => {
  api.fetchAPI.mockReset()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('DefenseLineBanner · 持仓行（防线）必须能被别处更新', () => {
  it('别处把防线从 34 改到 30 → 立刻重取持仓行，告警条不再报破位', async () => {
    let defense: number | null = 34
    mockApi({ summary: () => withDefense(defense) })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    expect(screen.getByTestId('defense-line-banner').textContent).toContain('已击穿')

    const before = summaryCalls()
    defense = 30
    await act(async () => {
      notifyDefenseLinesChanged()
      await Promise.resolve()
    })
    await flush()

    expect(summaryCalls()).toBeGreaterThan(before)
    const banner = screen.getByTestId('defense-line-banner').textContent ?? ''
    expect(banner).not.toContain('已击穿')
    expect(banner).toContain('¥30')
  })

  it('跨标签页的 storage 广播也能触发重取（其他页面保存后同样收到）', async () => {
    let defense: number | null = 34
    mockApi({ summary: () => withDefense(defense) })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    const before = summaryCalls()

    defense = 30
    await act(async () => {
      // 别的标签页写入 storage ⇒ 本页收到 storage 事件（浏览器只在"别的标签页"触发）
      window.localStorage.setItem(DEFENSE_LINES_STORAGE_KEY, '2026-10-29T10:00:00Z:9')
      window.dispatchEvent(
        new StorageEvent('storage', {
          key: DEFENSE_LINES_STORAGE_KEY,
          newValue: '2026-10-29T10:00:00Z:9',
        }),
      )
      await Promise.resolve()
    })
    await flush()

    expect(summaryCalls()).toBeGreaterThan(before)
    expect(screen.getByTestId('defense-line-banner').textContent ?? '').not.toContain('已击穿')
  })

  it('同文档 window 事件也能触发重取（嵌入页/微前端通道）', async () => {
    let defense: number | null = 34
    mockApi({ summary: () => withDefense(defense) })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    const before = summaryCalls()

    defense = 30
    await act(async () => {
      window.dispatchEvent(new CustomEvent(DEFENSE_LINES_CHANGED_EVENT))
      await Promise.resolve()
    })
    await flush()

    expect(summaryCalls()).toBeGreaterThan(before)
    expect(screen.getByTestId('defense-line-banner').textContent ?? '').not.toContain('已击穿')
  })

  it('无关 storage 写入不触发重取（不能变成"任何 localStorage 变化都重拉持仓"）', async () => {
    mockApi({ summary: () => withDefense(34) })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    const before = summaryCalls()

    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'sida_unrelated', newValue: 'x' }))
      await Promise.resolve()
    })
    await flush()

    expect(summaryCalls()).toBe(before)
  })

  it('保存防线后广播出去（其他页面/标签页因此收到）', async () => {
    window.localStorage.removeItem(DEFENSE_LINES_STORAGE_KEY)
    mockApi({ summary: () => withDefense(34) })
    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()

    // 打开设置对话框 → 保存
    await act(async () => {
      ;(screen.getByText('修改') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    await flush(4)
    const input = screen.getByTestId('defense-line-input') as HTMLInputElement
    await act(async () => {
      input.value = '30'
      input.dispatchEvent(new Event('input', { bubbles: true }))
      await Promise.resolve()
    })
    // 保存前：跨标签页通道还没有任何修订
    expect(window.localStorage.getItem(DEFENSE_LINES_STORAGE_KEY)).toBeNull()
    await act(async () => {
      ;(screen.getByTestId('defense-line-save') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    await flush(6)

    // 跨标签页通道真的写了修订号（同页 storage 监听不会自我回调，故直接看写入值）
    expect(window.localStorage.getItem(DEFENSE_LINES_STORAGE_KEY)).toBeTruthy()
  })

  it('没有事件时按 DEFENSE_QUOTE_REFRESH_MS 自己续期持仓行（兜底：助手/脚本改动）', async () => {
    vi.useFakeTimers()
    let defense: number | null = 34
    mockApi({ summary: () => withDefense(defense) })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    expect(screen.getByTestId('defense-line-banner').textContent).toContain('已击穿')

    defense = 30
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_QUOTE_REFRESH_MS)
    })
    await flush()

    expect(screen.getByTestId('defense-line-banner').textContent ?? '').not.toContain('已击穿')
  })

  it('持仓被删除 → 明说"不在持仓中"，不许继续用旧防线报警', async () => {
    vi.useFakeTimers()
    let deleted = false
    mockApi({ summary: () => (deleted ? NO_POSITION : withDefense(34)) })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    expect(screen.getByTestId('defense-line-banner').textContent).toContain('已击穿')

    deleted = true
    await act(async () => {
      notifyDefenseLinesChanged()
      await Promise.resolve()
    })
    await flush()

    expect(screen.queryByText(/已击穿你的防守线/)).toBeNull()
    const gone = screen.getByTestId('defense-line-unknown')
    expect(gone.textContent).toContain('不在持仓中')
  })

  it('持仓行续期失败 → 降级「时效未核实」，不拿旧防线继续报警', async () => {
    vi.useFakeTimers()
    let fail = false
    mockApi({ summary: () => (fail ? new Error('summary down') : withDefense(34)) })

    render(<DefenseLineBanner symbol="600111" cost={36.52} hasPosition />)
    await flush()
    expect(screen.getByTestId('defense-line-banner').textContent).toContain('已击穿')

    fail = true
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DEFENSE_QUOTE_REFRESH_MS)
    })
    await flush()

    expect(screen.queryByText(/已击穿你的防守线/)).toBeNull()
    const stale = screen.getByTestId('defense-line-stale')
    expect(stale.textContent).toContain('时效未核实')
  })
})
