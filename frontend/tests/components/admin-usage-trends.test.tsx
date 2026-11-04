// @vitest-environment jsdom
//
// 管理后台 · API Key 用量监控(2026-11-04): 新接口 `/admin/analytics/usage-trends` 的前端接入。
//
// 本用例走**真实组件 + 真实 useApiQuery**, 只 mock 网络层(@sida/api.fetchAPI), 钉住四件事:
//  ① 7/30 天切换各自打到 `?days=N`, 且当前只发一个窗口的请求(不是两个都拉);
//  ② qps 的 10s / 60s 均值上屏; 旧后端不返回 `peak_qps` 时显示 `--` 并说明原因, **不写 0**;
//  ③ `daily_trend` / `top_ips` 缺失时是明确的"未返回"空态, 不补一行 0;
//  ④ 后端显式给的 0 照实显示(真实零调用 ≠ 字段缺失), 以及失败态可重试;
//  ⑤ Key 管理页顶部直接有概览与入口, 点击写入 `section=sec-usage` 并保留其他查询参数。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  calls: [] as string[],
  fail: null as string | null,
  payload: {} as Record<string, unknown>,
}))

vi.mock('@sida/api', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    fetchAPI: (path: string) => {
      api.calls.push(path)
      if (api.fail) return Promise.reject(new Error(api.fail))
      const days = /days=(\d+)/.exec(path)?.[1] ?? '7'
      return Promise.resolve(api.payload[`d${days}`] ?? api.payload.default)
    },
  }
})

import { USAGE_SECTION_ID, UsageTrendsOverview, UsageTrendsPanel } from '@/components/admin/UsageTrendsPanel'

const FULL = {
  success: true,
  days: 7,
  qps: { current_10s: 0.3, avg_60s: 0.12, peak_qps: 1.75 },
  daily_trend: [
    { date: '2026-11-03', new_keys: 3, total_calls: 1234 },
    { date: '2026-11-04', new_keys: 0, total_calls: 0 },
  ],
  top_skills: [
    { skill_name: 'get_stock_quote', display_name: '实时行情', count: 900 },
    { skill_name: 'get_kline', display_name: null, count: 334 },
  ],
  top_ips: [
    { ip: '10.0.0.7', count: 640 },
    { ip: '10.0.0.9', count: 3 },
  ],
  total_calls_in_window: 1234,
}

function mount(ui: React.ReactElement, entry = '/admin?section=sec-keys') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } } })
  function LocationProbe() {
    const loc = useLocation()
    return <div data-testid="loc">{loc.pathname + loc.search}</div>
  }
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={client}>
        <LocationProbe />
        {ui}
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  api.calls = []
  api.fail = null
  api.payload = { default: FULL, d7: FULL, d30: { ...FULL, days: 30, daily_trend: [] } }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('用量监控 · 实时 QPS 与走势', () => {
  it('把 10 秒 / 60 秒均值与峰值上屏, 且只拉当前窗口(7 天)一次', async () => {
    mount(<UsageTrendsPanel />)
    // 先有明确的加载态
    expect(screen.getByRole('status').textContent).toContain('加载 7 天用量走势')
    await screen.findByText('当前 QPS（10 秒均值）')

    const strip = screen.getByText('当前 QPS（10 秒均值）').closest('dl') as HTMLElement
    expect(within(strip).getByText('0.30')).toBeTruthy()
    expect(within(strip).getByText('0.12')).toBeTruthy()
    expect(within(strip).getByText('1.75')).toBeTruthy()
    expect(within(strip).getByText('1,234')).toBeTruthy()

    expect(api.calls).toEqual(['/admin/analytics/usage-trends?days=7'])
  })

  it('每日走势按日期给出新增 Key 与总调用, 并保留后端显式的 0', async () => {
    mount(<UsageTrendsPanel />)
    await screen.findByText('每日新增 Key 与总调用')

    const row = screen.getByText('2026-11-04').closest('tr') as HTMLElement
    // 0 是后端真实读数(那天确实没新增/没调用), 必须显示 0 而不是 `--`
    expect(within(row).getAllByText('0')).toHaveLength(2)

    const yesterday = screen.getByText('2026-11-03').closest('tr') as HTMLElement
    expect(within(yesterday).getByText('1,234')).toBeTruthy()
    // 0 次调用的条形必须是空条: 不能为了"看得见"给它一个最小宽度, 那会把零画成有量
    const zeroBar = row.querySelector('span[style]') as HTMLElement
    expect(zeroBar.style.width).toBe('0%')
    const fullBar = yesterday.querySelector('span[style]') as HTMLElement
    expect(fullBar.style.width).toBe('100%')
  })

  it('Top Skills 用后端中文名, 缺映射时退回原始接口名', async () => {
    mount(<UsageTrendsPanel />)
    await screen.findByText('Top Skills（近 7 天调用最多）')
    expect(screen.getByText('实时行情')).toBeTruthy()
    expect(screen.getAllByText('get_stock_quote').length).toBeGreaterThan(0)
    expect(screen.getByText('部分接口缺少中文映射（display_name 为空），直接显示原始接口名。')).toBeTruthy()
  })

  it('Top IP 逐条列出并带调用次数', async () => {
    mount(<UsageTrendsPanel />)
    await screen.findByText('Top 客户端 IP（近 7 天）')
    const row = screen.getByText('10.0.0.7').closest('tr') as HTMLElement
    expect(within(row).getByText('640')).toBeTruthy()
  })
})

describe('用量监控 · 字段缺失不造 0', () => {
  it('旧后端不返回 peak_qps 时峰值显示 --, 并说明原因', async () => {
    api.payload = {
      default: { ...FULL, qps: { current_10s: 0.3, avg_60s: 0.12 } },
      d7: { ...FULL, qps: { current_10s: 0.3, avg_60s: 0.12 } },
    }
    mount(<UsageTrendsPanel />)
    await screen.findByText('峰值 QPS')

    const strip = screen.getByText('峰值 QPS').closest('dl') as HTMLElement
    expect(within(strip).getByText('--')).toBeTruthy()
    expect(within(strip).queryByText('0.00')).toBeNull()
    expect(screen.getByText(/后端本次未返回 `peak_qps`/)).toBeTruthy()
  })

  it('缺 daily_trend 时是"未返回逐日数据"空态, 不渲染 0 行', async () => {
    api.payload = { default: { ...FULL, daily_trend: undefined }, d7: { ...FULL, daily_trend: undefined } }
    mount(<UsageTrendsPanel />)
    await screen.findByText('接口未返回逐日数据')
    expect(screen.queryByText('2026-11-04')).toBeNull()
    // 窗口合计也不许变成 0
    const meta = screen.getByText(/窗口合计/, { exact: false })
    expect(meta.textContent).toContain('--')
    expect(meta.textContent).not.toContain('调用 0')
  })

  it('缺 top_ips / top_skills 时给出明确空态而不是空白或 0', async () => {
    api.payload = { default: { ...FULL, top_ips: undefined, top_skills: undefined }, d7: { ...FULL, top_ips: undefined, top_skills: undefined } }
    mount(<UsageTrendsPanel />)
    await screen.findByText('没有 IP 记录')
    expect(screen.getByText('窗口内暂无调用记录')).toBeTruthy()
  })
})

describe('用量监控 · 7/30 天切换与失败重试', () => {
  it('切到近 30 天会请求 days=30, 并渲染该窗口数据', async () => {
    mount(<UsageTrendsPanel />)
    await screen.findByText('当前 QPS（10 秒均值）')

    // Radix Tabs 在 mousedown/聚焦时激活(与真实点击一致), 单纯 click 不触发切换
    fireEvent.mouseDown(screen.getByRole('tab', { name: '近 30 天' }))
    await waitFor(() => expect(api.calls).toContain('/admin/analytics/usage-trends?days=30'))
    // 30 天窗口 payload 没有 daily_trend → 空态(证明渲染的是 30 天那份, 不是 7 天的残留)
    await screen.findByText('接口未返回逐日数据')
    expect(screen.getByText('Top Skills（近 30 天调用最多）')).toBeTruthy()
  })

  it('接口失败给出可重试的错误态, 重试会再发请求', async () => {
    api.fail = 'HTTP 500 用量服务不可用'
    mount(<UsageTrendsPanel />)
    await screen.findByText('用量数据加载失败')
    const retry = screen.getByRole('button', { name: '重试' })

    api.fail = null
    fireEvent.click(retry)
    await screen.findByText('当前 QPS（10 秒均值）')
    expect(api.calls.length).toBeGreaterThanOrEqual(2)
  })
})

describe('API Key 管理页 · 用量概览与入口', () => {
  it('Key 页直接显示 QPS / 7 天新增 Key / 总调用', async () => {
    mount(<UsageTrendsOverview />)
    const strip = (await screen.findByText('当前 QPS（10 秒）')).closest('dl') as HTMLElement
    expect(within(strip).getByText('0.30')).toBeTruthy()
    expect(within(strip).getByText('3')).toBeTruthy()       // 近 7 天新增 Key 合计
    expect(within(strip).getByText('1,234')).toBeTruthy()   // 近 7 天总调用合计
  })

  it('点击入口写入 section=sec-usage 并保留原有查询参数', async () => {
    mount(<UsageTrendsOverview />, '/admin?tab=ops&section=sec-keys')
    fireEvent.click(await screen.findByRole('button', { name: /查看完整用量监控/ }))

    await waitFor(() => {
      const search = new URLSearchParams(screen.getByTestId('loc').textContent?.split('?')[1] ?? '')
      expect(search.get('section')).toBe(USAGE_SECTION_ID)
      expect(search.get('tab')).toBe('ops')
    })
  })

  it('概览接口失败时不显示任何读数, 只给失败原因与重试', async () => {
    api.fail = 'HTTP 403 需要管理员权限'
    mount(<UsageTrendsOverview />)
    await screen.findByText(/用量接口读取失败/)
    expect(screen.queryByText('当前 QPS（10 秒）')).toBeNull()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()
  })
})

describe('Admin 页接线', () => {
  const raw = readFileSync(resolve(__dirname, '../../src/pages/Admin.tsx'), 'utf-8')
  const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  const keySection = src.slice(src.indexOf('id="sec-keys"'), src.indexOf('id="sec-usage"'))
  const usageSection = src.slice(src.indexOf('id="sec-usage"'), src.indexOf('id="sec-pro"'))

  it('API Key 管理分类里挂了用量概览(截图那页要能直接看到)', () => {
    expect(keySection).toContain('<UsageTrendsOverview />')
  })

  it('用量监控分类里挂了完整走势面板', () => {
    expect(usageSection).toContain('<UsageTrendsPanel />')
  })
})
