// @vitest-environment jsdom
//
// 前端 × 后端 **真实 payload 契约**用例(2026-11-04)。
//
// `tests/fixtures/usage-trends.sample.json` 是 `GET /api/admin/analytics/usage-trends?days=7`
// 的**真实响应 data 体**(由后端 `tests/test_usage_trends_contract.py::test_重生契约fixture`
// 导出的, 不是手写 mock)。本文件只 mock 网络层, 其余走真实组件与真实 useApiQuery。
//
// 为什么需要它: 上一版前端单测用的是一份手写 mock(`qps.peak_qps`), 与后端真实字段
// `qps.peak_per_sec` 不一致 —— 用例全绿, 线上峰值却永远 `--`。契约用例保证
// **同一份后端响应**在前端能渲染出峰值/日期/接口名/IP, 而不是掉进缺字段分支。
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ calls: [] as string[], payload: {} as Record<string, unknown> }))

vi.mock('@sida/api', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    fetchAPI: (path: string) => {
      api.calls.push(path)
      return Promise.resolve(api.payload.default)
    },
  }
})

import { normalizeUsageTrends, UsageTrendsPanel, type UsageTrendsRaw } from '@/components/admin/UsageTrendsPanel'

const FIXTURE_PATH = resolve(__dirname, '../fixtures/usage-trends.sample.json')
const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8')) as UsageTrendsRaw

function mount(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } } })
  return render(
    <MemoryRouter initialEntries={['/admin?section=sec-usage']}>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('契约 · 真实后端 payload 能被前端完整消费', () => {
  it('fixture 是后端真实字段: 有 peak_per_sec, 没有 peak_qps', () => {
    expect(raw.qps).toBeTruthy()
    expect(raw.qps && 'peak_per_sec' in raw.qps).toBe(true)
    expect(raw.qps && Object.prototype.hasOwnProperty.call(raw.qps, 'peak_qps')).toBe(false)
    expect(Array.isArray(raw.daily_trend)).toBe(true)
    expect(Array.isArray(raw.top_skills)).toBe(true)
    expect(Array.isArray(raw.top_ips)).toBe(true)
  })

  it('normalizeUsageTrends 不把真实字段降级成 null', () => {
    const view = normalizeUsageTrends(raw)
    expect(view.days).toBe(raw.days)
    expect(view.qps.current10s).not.toBeNull()
    expect(view.qps.avg60s).not.toBeNull()
    // 关键回归: peak 必须来自 peak_per_sec, 不是 null
    expect(view.qps.peak).toBe(raw.qps?.peak_per_sec)
    expect(view.qps.peakSecond).toBe(raw.qps?.peak_second)
    expect(view.daily.length).toBe(raw.daily_trend?.length)
    expect(view.daily.every((row) => /^\d{4}-\d{2}-\d{2}$/.test(row.date))).toBe(true)
    expect(view.skills.length).toBe(raw.top_skills?.length)
    expect(view.skills.some((row) => row.known === true)).toBe(true)
    expect(view.skills.some((row) => row.known === false)).toBe(true)
    expect(view.ips.length).toBe(raw.top_ips?.length)
  })

  it('真实响应渲染出 QPS 峰值与时间, 不是 --', async () => {
    api.payload = { default: raw }
    mount(<UsageTrendsPanel />)

    const strip = (await screen.findByText('当前 QPS（10 秒均值）')).closest('dl') as HTMLElement
    const c10 = Number(raw.qps?.current_10s).toFixed(2)
    const a60 = Number(raw.qps?.avg_60s).toFixed(2)
    expect(within(strip).getByText(c10)).toBeTruthy()
    expect(within(strip).getByText(a60)).toBeTruthy()
    // 峰值按后端整数显示(单秒调用数)
    expect(within(strip).getByText(String(raw.qps?.peak_per_sec))).toBeTruthy()
    expect(within(strip).queryByText('--')).toBeNull()

    const hhmmss = /(\d{2}:\d{2}:\d{2})/.exec(String(raw.qps?.peak_second))?.[1]
    expect(hhmmss).toBeTruthy()
    expect(screen.getByText(new RegExp(`峰值出现在 ${hhmmss}`))).toBeTruthy()
    // 字段齐全时不该出现"接口未返回"提示
    expect(screen.queryByText(/后端本次未返回 `qps.peak_per_sec`/)).toBeNull()
  })

  it('每日序列按 fixture 的日期原样渲染, 合计等于后端窗口总量', async () => {
    api.payload = { default: raw }
    mount(<UsageTrendsPanel />)
    await screen.findByText('每日新增 Key 与总调用')

    const rows = (raw.daily_trend ?? []).map((row) => row.date as string)
    for (const date of [rows[0], rows[rows.length - 1]]) {
      expect(screen.getByText(date)).toBeTruthy()
    }
    // 一行一天, 不缺不补
    expect(screen.getAllByText(/^\d{4}-\d{2}-\d{2}$/).length).toBe(rows.length)
    const total = (raw.daily_trend ?? []).reduce((acc, row) => acc + Number(row.total_calls), 0)
    const meta = screen.getByText(/窗口合计/, { exact: false })
    expect(meta.textContent).toContain(`调用 ${total.toLocaleString('zh-CN')}`)
  })

  it('Top Skills 用后端 display_name, 未收录的 known=false 有明确说明', async () => {
    api.payload = { default: raw }
    mount(<UsageTrendsPanel />)
    await screen.findByText('Top Skills（近 7 天调用最多）')

    for (const row of raw.top_skills ?? []) {
      if (row.display_name) expect(screen.getAllByText(row.display_name).length).toBeGreaterThan(0)
    }
    const unknown = (raw.top_skills ?? []).filter((row) => row.known === false)
    if (unknown.length > 0) {
      expect(screen.getByText(new RegExp(`有 ${unknown.length} 个接口未收录中文映射`))).toBeTruthy()
    }
  })

  it('Top IP 与后端一致(含 unknown 档)', async () => {
    api.payload = { default: raw }
    mount(<UsageTrendsPanel />)
    await screen.findByText('Top 客户端 IP（近 7 天）')
    for (const row of raw.top_ips ?? []) {
      expect(screen.getAllByText(row.ip as string).length).toBeGreaterThan(0)
    }
  })
})
