// @vitest-environment jsdom
//
// 入口扶正(2026-10-23) 回归钉: 盘前/盘后分析入口
//   1. ReportsHub 默认打开「分析」Tab(history 内容),「底稿归档」排第二位(?tab= 兼容不变);
//   2. 点「▶ 立即分析」→ premarketApi.run() 入队 → 按钮转「分析生成中…」并禁用(防连点);
//   3. run 429/报错 → 显式内联展示原因(不静默吞错), 按钮可重试;
//   4. 点「问参谋」→ 跳 /ai-copilot?agent=premarket_strategist&q=<编码问题>;
//   5. Dashboard「盘前 / 盘后分析」卡: 空态 / 有数据+is_stale 基准日显式标注 / 加载失败显式错误。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// ── 可控桩 ────────────────────────────────────────────────────────────────────
const P = vi.hoisted(() => ({
  getTodayCalls: 0,
  getTodayQueue: [] as unknown[],
  getTodayDefault: null as unknown,
  runCalls: 0,
  runImpl: null as null | (() => Promise<unknown>),
  copilotSearch: '' as string,
}))

const H = vi.hoisted(() => {
  const queued: Record<string, Array<{ resolve: (v: unknown) => void; reject: (e: unknown) => void }>> = {}
  return {
    clear: () => {
      for (const k of Object.keys(queued)) delete queued[k]
    },
    defer: (key: string) => {
      let resolve!: (v: unknown) => void
      let reject!: (e: unknown) => void
      const promise = new Promise((res, rej) => {
        resolve = res
        reject = rej
      })
      ;(queued[key] ||= []).push({ resolve, reject })
      void promise
      return promise
    },
    pendingCount: (key: string) => (queued[key] || []).length,
    settle: (key: string, value: unknown) => {
      const d = (queued[key] || []).shift()
      if (d) d.resolve(value)
    },
    /** 快车道 6 接口一次性放行(空数据) */
    settleFastLane: async () => {
      H.settle('scan', { stocks: [] })
      H.settle('overview', { action_center: { opportunities: [] } })
      H.settle('diag', { position_count: 0, by_market: {}, alerts: [], total_market_value: 0 })
      H.settle('alerts', [])
      H.settle('todos', { todos: [] })
      H.settle('status', [])
      await Promise.resolve()
    },
  }
})

// ── @sida/api: 全可控(ReportsHub 用 premarket, Dashboard 用快车道队列) ────
vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  fetchAPI: () => Promise.resolve([]),
  reportsApi: { list: () => Promise.resolve({ items: [] }) },
  dashboardApi: {
    indices: () => H.defer('indices'),
    marketCapitalFlow: () => H.defer('flow'),
    anomalies: () => Promise.resolve({ items: [] }),
    intradayScan: () => H.defer('scan'),
    overview: () => H.defer('overview'),
    marketStatus: () => H.defer('status'),
    curate: () => Promise.resolve({ items: [] }),
    watchlist: () => Promise.resolve([]),
    portfolioSummary: () => Promise.resolve(null),
  },
  portfolioApi: {
    diagnostics: () => H.defer('diag'),
    benchmark: () => H.defer('bench'),
    attribution: () => H.defer('attr'),
    aiReview: () => Promise.resolve(null),
  },
  recommendationsApi: { listStrategySignals: () => Promise.resolve({ items: [] }) },
  homeApi: { alertHitsToday: () => H.defer('alerts'), todos: () => H.defer('todos') },
  stocksApi: { list: () => Promise.resolve([]), create: () => Promise.resolve({}) },
  premarketApi: {
    getToday: () => {
      P.getTodayCalls++
      const v = P.getTodayQueue.length > 0 ? P.getTodayQueue.shift() : P.getTodayDefault
      return v instanceof Error ? Promise.reject(v) : Promise.resolve(v)
    },
    run: () => {
      P.runCalls++
      const impl = P.runImpl ?? (() => Promise.resolve({ queued: true }))
      return impl()
    },
    getEvidence: () => Promise.resolve({ available: false, blocks: [] }),
  },
}))

// ── Dashboard 重区块打桩(jsdom 无 canvas, 只关心不崩 + 首屏不阻塞) ───────────
vi.mock('@sida/biz-ui/components/dashboard/BreadthDistributionChart', () => ({
  default: () => <div data-testid="breadth-chart" />,
}))
vi.mock('@sida/biz-ui/components/dashboard/SentimentGauge', () => ({
  default: () => <div data-testid="sentiment-gauge" />,
}))
vi.mock('@sida/biz-ui/components/dashboard/FlowHistoryChart', () => ({
  default: () => <div data-testid="flow-chart" />,
}))
vi.mock('@sida/biz-ui/components/dashboard/ResonancePanel', () => ({
  default: () => <div data-testid="resonance-panel" />,
}))
vi.mock('@sida/biz-ui/components/MarketPhaseCard', () => ({
  default: () => <div data-testid="market-phase-card" />,
}))
vi.mock('@sida/biz-ui/components/MarketMainlineCard', () => ({
  default: () => <div data-testid="market-mainline-card" />,
}))
vi.mock('@sida/biz-ui/components/KpiBand', () => {
  const PHASE = { label: null, loading: true, error: null, unavailableNote: null, limitUp: null, sealRate: null, reload: () => {} }
  const MAINLINE = { top: null, loading: true, error: null }
  const Stub = () => <div data-testid="kpi-band" />
  return { default: Stub, usePhaseLabel: () => PHASE, useMainlineTop1: () => MAINLINE }
})
vi.mock('@sida/biz-ui/components/AnimatedNumber', () => ({
  default: ({ value }: { value?: ReactNode }) => <span>{String(value)}</span>,
}))
vi.mock('@sida/biz-ui/components/FlashValue', () => ({
  default: ({ children }: { children?: ReactNode }) => <>{children}</>,
}))
vi.mock('@sida/biz-ui/components/SectionHeader', () => ({
  default: ({ title, action }: { title?: ReactNode; action?: ReactNode }) => (
    <div>
      {title}
      {action}
    </div>
  ),
}))
vi.mock('@sida/biz-ui/components/Stat', () => ({
  default: ({ label, value }: { label?: ReactNode; value?: ReactNode }) => (
    <div>
      {label}
      {value}
    </div>
  ),
}))
vi.mock('@sida/biz-ui/components/CaliberBadge', () => ({
  default: () => <span data-testid="caliber-badge" />,
  caliberOf: () => 'unknown',
}))
vi.mock('@sida/biz-ui/components/onboarding', () => ({ Onboarding: () => null }))
vi.mock('@sida/base-ui/components/ui/button', () => ({
  Button: ({ children, ...rest }: { children?: ReactNode } & Record<string, unknown>) => {
    const { onClick, title, disabled, 'aria-label': ariaLabel } = rest as {
      onClick?: () => void
      title?: string
      disabled?: boolean
      'aria-label'?: string
    }
    return (
      <button onClick={onClick} title={title} disabled={disabled} aria-label={ariaLabel}>
        {children}
      </button>
    )
  },
}))
vi.mock('@/components/Skeleton', () => ({ Skeleton: () => <div /> }))
vi.mock('@/components/SkeletonRows', () => ({ default: () => <div data-testid="skeleton-rows" /> }))
vi.mock('@/components/Sparkline', () => ({ default: () => <div /> }))
vi.mock('@/components/SafeMarkdown', () => ({ default: () => null }))
vi.mock('@/components/ScanJobButton', () => ({ default: () => null }))
vi.mock('@/components/MarketStatusPills', () => ({ default: () => <div data-testid="market-status" /> }))
vi.mock('@/components/BenchChart', () => ({ default: () => <div data-testid="bench-chart" /> }))
vi.mock('@/components/BenchmarkShareCard', () => ({ default: () => null }))
vi.mock('@/components/DiagnosticsShareCard', () => ({ default: () => null }))
vi.mock('@/components/DigestShareCard', () => ({ default: () => null }))
vi.mock('@/components/StockContextMenu', () => ({ default: () => null }))
vi.mock('@/components/DiscoveryPanel', () => ({ default: () => <div data-testid="discovery-panel" /> }))
vi.mock('@/components/DashboardCustomizer', () => ({
  default: ({ open }: { open?: boolean }) => <div data-testid="customizer" data-open={String(!!open)} />,
}))
vi.mock('@/components/ErrorBanner', () => ({
  default: ({ errors }: { errors: Array<{ source: string }>; retryAll?: () => void }) => (
    <div data-testid="error-banner">{errors.map((e) => e.source).join(',')}</div>
  ),
}))
vi.mock('@/hooks/useI18n', () => {
  const t = (key: string) => key
  return { useI18n: () => ({ locale: 'zh-CN', setLocale: () => {}, t, dict: {} }) }
})

import ReportsHubPage from '../../src/pages/ReportsHub'
import DashboardPage from '../../src/pages/Dashboard'

const ASK_QUERY = '按今天的盘前分析，帮我梳理主线与持仓关键位并画到图上'

function CopilotProbe() {
  const location = useLocation()
  P.copilotSearch = location.search
  return <div data-testid="copilot-probe" />
}

function renderHub() {
  return render(
    <MemoryRouter initialEntries={['/reports']}>
      <Routes>
        <Route path="/reports" element={<ReportsHubPage />} />
        <Route path="/ai-copilot" element={<CopilotProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

function renderDashboard() {
  return render(
    <MemoryRouter>
      <DashboardPage />
    </MemoryRouter>,
  )
}

/** Dashboard 首屏 settle(loading 翻 false → 盘前卡才取数) */
async function settleFirstScreen() {
  await act(async () => {
    await H.settleFastLane()
    await Promise.resolve()
  })
}

beforeEach(() => {
  localStorage.clear()
  H.clear()
  P.getTodayCalls = 0
  P.getTodayQueue = []
  P.getTodayDefault = null
  P.runCalls = 0
  P.runImpl = null
  P.copilotSearch = ''
})
afterEach(() => cleanup())

describe('ReportsHub 盘前入口(入口扶正 2026-10-23)', () => {
  it('默认打开「报告中心」Tab(原 history 内容, L3-D 改名),「底稿归档」排第二位', async () => {
    renderHub()
    // 默认 Tab = 分析: history 页空态落地(不要求 URL 带 ?tab=)
    await screen.findByText('暂无分析记录')
    // 两个 Tab 都在: 「报告中心」默认选中,「底稿归档」在第二位未选中
    const analysisTab = screen.getByRole('tab', { name: '报告中心' })
    expect(analysisTab.getAttribute('aria-selected')).toBe('true')
    const archiveTab = screen.getByRole('tab', { name: '底稿归档' })
    expect(archiveTab.getAttribute('aria-selected')).toBe('false')
  })

  it('点「▶ 立即分析」触发 premarketApi.run(), 入队后转「分析生成中…」并禁用防连点', async () => {
    renderHub()
    const btn = await screen.findByTestId('premarket-run-btn')
    expect(btn.textContent).toContain('立即分析')

    fireEvent.click(btn)
    await waitFor(() => expect(P.runCalls).toBe(1))
    await waitFor(() => expect(btn.textContent).toContain('分析生成中…'))
    expect((btn as HTMLButtonElement).disabled).toBe(true)

    // 连点不重发
    fireEvent.click(btn)
    fireEvent.click(btn)
    expect(P.runCalls).toBe(1)
  })

  it('run 返回 429/报错 → 显式内联展示原因, 按钮可重试(不静默吞错)', async () => {
    P.runImpl = () => {
      const e = new Error('Too many requests')
      Object.assign(e, { status: 429 })
      return Promise.reject(e)
    }
    renderHub()
    const btn = await screen.findByTestId('premarket-run-btn')
    fireEvent.click(btn)

    const err = await screen.findByTestId('premarket-run-error')
    expect(err.textContent).toContain('429')
    expect(err.textContent).toContain('Too many requests')
    // 错误态可重试: 按钮重新可用, 再点再发
    expect((btn as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(btn)
    await waitFor(() => expect(P.runCalls).toBe(2))
  })

  it('点「问参谋」跳 /ai-copilot, URL 带 agent + 编码后的 q', async () => {
    renderHub()
    fireEvent.click(await screen.findByTestId('premarket-ask-copilot-btn'))

    const probe = await screen.findByTestId('copilot-probe')
    expect(probe).toBeTruthy()
    expect(P.copilotSearch).toContain('agent=premarket_strategist')
    expect(P.copilotSearch).toContain(`q=${encodeURIComponent(ASK_QUERY)}`)
  })
})

describe('Dashboard「盘前 / 盘后分析」卡', () => {
  it('空态: 显示「今日盘前分析尚未生成」+「立即分析」按钮', async () => {
    P.getTodayDefault = { date: '2026-10-23', empty: true }
    renderDashboard()
    // 首屏未 settle 前: 卡处于骨架态, 且首帧请求不受影响
    // L2-A(2026-10-24): KpiBand 已从 Dashboard 移除(数字并排→结论行), 首屏锚点换成结论行
    expect(screen.getByTestId('market-conclusion')).toBeTruthy()

    await settleFirstScreen()
    await screen.findByText('今日盘前分析尚未生成')
    expect(screen.getByTestId('premarket-run-now')).toBeTruthy()
    expect(P.getTodayCalls).toBe(1)
  })

  it('有数据 + is_stale: 显式标注「基准日 …(非今日)」, 展示门禁/主线/前3条预案', async () => {
    P.getTodayDefault = {
      date: '2026-10-22',
      empty: false,
      is_stale: true,
      title: '10-22 盘前分析',
      structured: {
        gate: { score: '强', position_cap: '60%' },
        mainlines: [
          { name: '电力', strength: '强' },
          { name: '半导体', strength: '中' },
        ],
        plans: [
          { symbol: '600000', name: '浦发银行', action: 'watch', trigger: '7.85', stop: '7.30' },
          { symbol: '000001', name: '平安银行', action: 'hold', trigger: '55.00' },
          { symbol: '000858', name: '贵州茅台', action: 'avoid' },
          { symbol: '002594', name: '比亚迪', action: 'alert' },
        ],
      },
    }
    renderDashboard()
    await settleFirstScreen()

    const stale = screen.getByTestId('premarket-stale-badge')
    expect(stale.textContent).toBe('基准日 2026-10-22（非今日）')
    expect(screen.getByText('环境门禁 强')).toBeTruthy()
    expect(screen.getByText('仓位上限 60%')).toBeTruthy()
    expect(screen.getByText(/电力/)).toBeTruthy()
    // 预案取前 3: 第 4 条(比亚迪)被截掉
    expect(screen.getByText('600000')).toBeTruthy()
    expect(screen.getByText('关注')).toBeTruthy()
    expect(screen.getByText('触发 7.85 / 止损 7.30')).toBeTruthy()
    expect(screen.queryByText('比亚迪')).toBeNull()
    // 有数据态不再出空态文案
    expect(screen.queryByText('今日盘前分析尚未生成')).toBeNull()
  })

  it('接口报错 → 显式「盘前分析加载失败」(不伪造空态)', async () => {
    P.getTodayDefault = new Error('premarket-down')
    renderDashboard()
    await settleFirstScreen()

    const err = await screen.findByTestId('premarket-card-error')
    expect(err.textContent).toBe('盘前分析加载失败')
    expect(screen.queryByText('今日盘前分析尚未生成')).toBeNull()
  })
})
