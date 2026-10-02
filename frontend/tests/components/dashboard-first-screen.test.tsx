// @vitest-environment jsdom
//
// perf(2026-10-02) 回归钉: 首页(`/`)首屏加载链 —— 先出壳后补数 / 慢车道错峰 / 渲染缓存 / 竞态守卫。
//
// 背景(B1 首页冷态走查): 冷态 14.3s 主内容才 settle。走查产物文件已被缓存清理删除, 故这里
// 全部结论来自**代码定位 + 本机 mock 探针**(非生产实测, 不代表线上绝对值):
//   - 首页冷启动并发 ~18 个请求(含「市场温度」/market/phase 被 3 个组件各打一次、
//     「主线」/market/mainline 被 2 个组件各打一次), 抢浏览器同域连接池 + 后端单 worker 队列;
//   - 分钟级的慢车道(基准/归因, 拉全持仓 K 线)与关键画布同刻挤入;
//   - 图表/列表区块未 memo, 父组件任意状态变更(30s 轮询/错误横幅/看板定制/分享弹窗)都整棵重渲染;
//   - load 无序号, 手动刷新与轮询重叠时旧响应可能覆盖新数据。
//
// 本测试全 mock(禁真实网络), 钉住四条契约:
//   1. 核心块(外壳/KPI/情绪周期卡/涨跌分布)不等任何慢接口, 首帧即挂载;
//   2. 慢车道(基准/归因)**不在**首屏窗口内发起, 且自身四态占位语义不变;
//   3. 接口失败 → 显式错误横幅, 且缺值一律 null(由卡片渲染 '--'), 绝不填 0 假装有数据;
//   4. load 竞态: 被更新的一轮取代后, 旧响应(含错误)**不得**落地;
//   5. 无关状态变更(打开看板定制)不重渲染自带画布/取数的重区块。
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const H = vi.hoisted(() => {
  const counters: Record<string, number> = {}
  const queued: Record<string, Array<{ resolve: (v: unknown) => void; reject: (e: unknown) => void; promise: Promise<unknown> }>> = {}
  return {
    counters,
    queued,
    inc: (k: string) => {
      counters[k] = (counters[k] || 0) + 1
    },
    resetCounters: () => {
      for (const k of Object.keys(counters)) delete counters[k]
    },
    defer: (key: string) => {
      let resolve!: (v: unknown) => void
      let reject!: (e: unknown) => void
      const promise = new Promise((res, rej) => {
        resolve = res
        reject = rej
      })
      ;(queued[key] ||= []).push({ resolve, reject, promise })
      return promise
    },
    pendingCount: (key: string) => (queued[key] || []).length,
    settle: (key: string, value: unknown) => {
      const list = queued[key] || []
      const d = list.shift()
      if (d) d.resolve(value)
    },
    fail: (key: string, err: unknown) => {
      const list = queued[key] || []
      const d = list.shift()
      if (d) d.reject(err)
    },
    clearQueues: () => {
      for (const k of Object.keys(queued)) delete queued[k]
    },
    /** 按队列位置放行(不 shift) —— 用于“后发的先回、先发的晚回”竞态场景 */
    resolveAt: (key: string, index: number, value: unknown) => {
      const d = (queued[key] || [])[index]
      if (d) d.resolve(value)
    },
    rejectAt: (key: string, index: number, err: unknown) => {
      const d = (queued[key] || [])[index]
      if (d) d.reject(err)
    },
    kpiProps: null as Record<string, unknown> | null,
    customizerOpen: false as boolean | null,
    errors: [] as Array<{ source: string }>,
  }
})

// ── 图表/重区块: 只计数 + 打桩(jsdom 无 canvas) ─────────────────────────────
vi.mock('@panwatch/biz-ui/components/dashboard/BreadthDistributionChart', () => ({
  default: () => {
    H.inc('breadth')
    return <div data-testid="breadth-chart" />
  },
}))
vi.mock('@panwatch/biz-ui/components/dashboard/SentimentGauge', () => ({
  default: () => {
    H.inc('sentiment')
    return <div data-testid="sentiment-gauge" />
  },
}))
vi.mock('@panwatch/biz-ui/components/dashboard/FlowHistoryChart', () => ({
  default: () => {
    H.inc('flow-chart')
    return <div data-testid="flow-chart" />
  },
}))
vi.mock('@panwatch/biz-ui/components/dashboard/ResonancePanel', () => ({
  default: () => {
    H.inc('resonance')
    return <div data-testid="resonance-panel" />
  },
}))
vi.mock('@panwatch/biz-ui/components/MarketPhaseCard', () => ({
  default: () => {
    H.inc('market-phase')
    return <div data-testid="market-phase-card" />
  },
}))
vi.mock('@panwatch/biz-ui/components/MarketMainlineCard', () => ({
  default: () => {
    H.inc('market-mainline')
    return <div data-testid="market-mainline-card" />
  },
}))
vi.mock('@panwatch/biz-ui/components/KpiBand', () => {
  // 稳定引用: memo 命中与否取决于 props 是否变(见首页 memo 化)
  const PHASE = { label: null, loading: true, error: null, unavailableNote: null, limitUp: null, sealRate: null, reload: () => {} }
  const MAINLINE = { top: null, loading: true, error: null }
  const Stub = (props: Record<string, unknown>) => {
    H.inc('kpi')
    H.kpiProps = props
    return <div data-testid="kpi-band" />
  }
  return { default: Stub, usePhaseLabel: () => PHASE, useMainlineTop1: () => MAINLINE }
})
vi.mock('@panwatch/biz-ui/components/AnimatedNumber', () => ({
  default: ({ value }: { value?: ReactNode }) => <span>{String(value)}</span>,
}))
vi.mock('@panwatch/biz-ui/components/FlashValue', () => ({
  default: ({ children }: { children?: ReactNode }) => <>{children}</>,
}))
vi.mock('@panwatch/biz-ui/components/SectionHeader', () => ({
  default: ({ title, action }: { title?: ReactNode; action?: ReactNode }) => (
    <div>
      {title}
      {action}
    </div>
  ),
}))
vi.mock('@panwatch/biz-ui/components/Stat', () => ({
  default: ({ label, value }: { label?: ReactNode; value?: ReactNode }) => (
    <div>
      {label}
      {value}
    </div>
  ),
}))
vi.mock('@panwatch/biz-ui/components/CaliberBadge', () => ({
  default: () => <span data-testid="caliber-badge" />,
  caliberOf: () => 'unknown',
}))
vi.mock('@panwatch/biz-ui/components/onboarding', () => ({ Onboarding: () => null }))
vi.mock('@panwatch/base-ui/components/ui/button', () => ({
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

// ── 首页局部组件 ────────────────────────────────────────────────────────────
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
vi.mock('@/components/DiscoveryPanel', () => ({
  default: () => {
    H.inc('discovery')
    return <div data-testid="discovery-panel" />
  },
}))
vi.mock('@/components/DashboardCustomizer', () => ({
  default: ({ open }: { open?: boolean }) => {
    H.customizerOpen = !!open
    return <div data-testid="customizer" data-open={String(!!open)} />
  },
}))
vi.mock('@/components/ErrorBanner', () => ({
  default: ({ errors, retryAll }: { errors: Array<{ source: string }>; retryAll?: () => void }) => {
    H.errors = errors
    return (
      <div data-testid="error-banner">
        {errors.map((e) => e.source).join(',')}
        {retryAll && errors.length > 0 ? (
          <button data-testid="retry-all" onClick={retryAll}>
            重试全部
          </button>
        ) : null}
      </div>
    )
  },
}))
vi.mock('@/hooks/useI18n', () => {
  const t = (key: string) => key
  return { useI18n: () => ({ locale: 'zh-CN', setLocale: () => {}, t, dict: {} }) }
})

// ── API(全部可控 deferred, 无真实网络) ───────────────────────────────────────
vi.mock('@panwatch/api', () => ({
  getToken: () => 'test-token',
  fetchAPI: () =>
    Promise.resolve({
      available: true,
      current: { phase: 'rally', label: '主升', max_height: 5, promo_rate: 0.3, seal_rate: 0.6, first_board: 60, ge2_count: 20 },
    }),
  dashboardApi: {
    indices: () => H.defer('indices'),
    marketCapitalFlow: () => H.defer('flow'),
    anomalies: () => H.defer('anomalies'),
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
    aiReview: () => H.defer('aiReview'),
  },
  recommendationsApi: { listStrategySignals: () => Promise.resolve({ items: [] }) },
  homeApi: { alertHitsToday: () => H.defer('alerts'), todos: () => H.defer('todos') },
  stocksApi: { list: () => H.defer('stocks'), create: () => Promise.resolve({}) },
  reportsApi: { list: () => H.defer('reports') },
}))

import DashboardPage from '../../src/pages/Dashboard'

function renderPage() {
  return render(
    <MemoryRouter>
      <DashboardPage />
    </MemoryRouter>,
  )
}

/** 冲掉若干已 resolve 的微任务 */
async function flush(times = 3) {
  await act(async () => {
    for (let i = 0; i < times; i++) await Promise.resolve()
  })
}

/** 把 6 个快车道接口一次性放行(空数据) */
async function settleFastLane() {
  await act(async () => {
    H.settle('scan', { stocks: [] })
    H.settle('overview', { action_center: { opportunities: [] } })
    H.settle('diag', { position_count: 0, by_market: {}, alerts: [], total_market_value: 0 })
    H.settle('alerts', [])
    H.settle('todos', { todos: [] })
    H.settle('status', [])
    await Promise.resolve()
  })
}

beforeEach(() => {
  localStorage.clear()
  H.resetCounters()
  H.clearQueues()
  H.kpiProps = null
  H.customizerOpen = false
  H.errors = []
})
afterEach(() => cleanup())

describe('Dashboard 首屏加载链(perf 回归)', () => {
  it('先出壳: 核心块首帧即挂载, 不等任何慢接口', () => {
    renderPage()
    // 未 await 任何请求: 外壳 + 关键区块已在 DOM
    expect(screen.getByTestId('kpi-band')).toBeTruthy()
    expect(screen.getByTestId('market-phase-card')).toBeTruthy()
    expect(screen.getByTestId('breadth-chart')).toBeTruthy()
    // 6 个快车道接口确实都发出去了且尚未返回
    for (const k of ['scan', 'overview', 'diag', 'alerts', 'todos', 'status']) {
      expect(H.pendingCount(k)).toBe(1)
    }
  })

  it('慢车道错峰: 快车道未回不发基准/归因; 回来后仍等错峰窗口再发', async () => {
    renderPage()
    expect(H.pendingCount('bench')).toBe(0)

    await settleFastLane()
    // 快车道已回, 但慢车道被错峰推迟 —— 不在关键首屏窗口抢连接/队列
    expect(H.pendingCount('bench')).toBe(0)
    expect(H.pendingCount('attr')).toBe(0)

    await act(async () => {
      await new Promise((r) => setTimeout(r, 1300))
    })
    expect(H.pendingCount('bench')).toBe(1)
    expect(H.pendingCount('attr')).toBe(1)
  })

  it('接口失败 → 显式错误横幅; 缺值一律 null(由卡片渲染 --), 不填 0 假装有数据', async () => {
    renderPage()
    // 资金流返回空对象(字段缺)
    H.settle('flow', {})
    H.fail('todos', new Error('todos-down'))
    H.fail('status', new Error('status-down'))
    await settleFastLane()
    await flush()

    // 失败显式可见(两个源)
    const banner = screen.getByTestId('error-banner')
    expect(banner.textContent).toContain('dashboard.errorSources.todos')
    expect(banner.textContent).toContain('dashboard.errorSources.marketStatus')
    // 缺值 → null, 不是 0
    expect(H.kpiProps?.upCount).toBeNull()
    expect(H.kpiProps?.downCount).toBeNull()
    expect(H.kpiProps?.mainFlowYi).toBeNull()
    expect(H.kpiProps?.amountYi).toBeNull()
  })

  it('竞态守卫: 被新一轮 load 取代后, 旧响应(含错误)不落地', async () => {
    renderPage()
    // load#1 的 indices 先挂着
    expect(H.pendingCount('indices')).toBe(1)

    // 先让快车道以一次失败结束(todos) → 错误横幅出现 + 重试入口(重试即再跑一轮 load)
    await act(async () => {
      H.settle('scan', { stocks: [] })
      H.settle('overview', { action_center: { opportunities: [] } })
      H.settle('diag', { position_count: 0, by_market: {}, alerts: [], total_market_value: 0 })
      H.settle('alerts', [])
      H.settle('status', [])
      H.fail('todos', new Error('todos-down'))
      await Promise.resolve()
    })
    expect(screen.getByTestId('error-banner').textContent).toContain('dashboard.errorSources.todos')

    // 重试全部 → load#2(又发一个 indices); 此时 load#1 的 indices 仍未回
    fireEvent.click(screen.getByTestId('retry-all'))
    expect(H.pendingCount('indices')).toBe(2)

    // 后发的那一轮(load#2, 队列 index 1)先回 → 成功
    await act(async () => {
      H.resolveAt('indices', 1, [])
      await Promise.resolve()
    })
    // 先发的旧响应(load#1, index 0)随后**失败** —— 已过期, 不得弹错误横幅
    await act(async () => {
      H.rejectAt('indices', 0, new Error('stale-down'))
      await Promise.resolve()
    })
    await flush()
    expect(screen.getByTestId('error-banner').textContent).not.toContain('dashboard.errorSources.indices')
  })

  it('无关状态变更(打开看板定制)不重渲染自带画布/取数的重区块', async () => {
    renderPage()
    await flush()
    // 市场温度卡取数就绪 → 内部 canvas 已渲染
    expect(screen.getByTestId('sentiment-gauge')).toBeTruthy()

    const before = { ...H.counters }
    fireEvent.click(screen.getByTitle('dashboard.customize'))

    // 状态确实变了(定制对话框 open)
    expect(H.customizerOpen).toBe(true)
    expect(screen.getByTestId('customizer').getAttribute('data-open')).toBe('true')
    // 但重区块一次都没重渲染
    for (const k of ['breadth', 'sentiment', 'market-phase', 'market-mainline', 'kpi', 'flow-chart']) {
      expect(H.counters[k]).toBe(before[k])
    }
  })
})
