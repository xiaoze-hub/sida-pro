// @vitest-environment jsdom
//
// 首页「主体」审计回归钉(home-audit-ui, 2026-11-08)。渲染**真** Dashboard 组件, 只把
// 画布类子组件与网络层打桩。每条用例对应生产上真见过(或审计点名)的问题:
//
//   A. 今日要紧事的 AI 说明挂到了别的股票上 —— 请求快照变了 / 响应乱序 / 说明点名别人
//      → 一律 fail closed, 退回原始信号; 原始候选一条不丢。
//   B. 今日要紧事与「机会精选」重复 —— 去重, 但机会模块本身不动。
//   C. 封板率 0.62 被渲染成 "1%" —— 比例→百分数。
//   D. 6 个指数只显示 5 个。
//   E. 市场全景表头读扁平 leader_name(接口从没给过) → 龙头永远缺失; 真实结构是嵌套 leader。
//   F. 资金流不标来源/快照时点; 缺时点必须写"时点未知", 不拿当前时间冒充。
//   G. 请求失败被渲染成"今日暂无明显异动 ✓"(假空态)。
//   H. 成本缺失(unpriced_positions)、风险项没接线。
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const S = vi.hoisted(() => {
  type Deferred = { promise: Promise<unknown>; resolve: (v: unknown) => void; reject: (e: unknown) => void; args: unknown }
  const state = {
    indices: [] as Array<Record<string, unknown>>,
    flow: {} as Record<string, unknown>,
    flowDeferred: false,
    scan: { stocks: [] as Array<Record<string, unknown>> } as unknown,
    scanFail: null as string | null,
    overview: {
      snapshot_date: '2026-11-07',
      data_freshness: {
        strategy_snapshot_date: '2026-11-07',
        entry_snapshot_date: '2026-11-07',
        market_scan_snapshot_date: '2026-11-07',
        latest_history_updated_at: '',
      },
      kpis: { watchlist_count: 3, unpriced_positions: 0 },
      action_center: { opportunities: [] as Array<Record<string, unknown>>, risk_items: [] as Array<Record<string, unknown>> },
    } as Record<string, unknown>,
    overviewFail: null as string | null,
    diag: {
      position_count: 2,
      total_market_value: 100,
      hhi: 0.4,
      max_weight: 0.6,
      by_market: { CN: 100 },
      by_strategy: {},
      total_unrealized_pnl: 1,
      alerts: [] as string[],
    } as Record<string, unknown>,
    diagFail: null as string | null,
    alerts: [] as Array<Record<string, unknown>>,
    alertsFail: null as string | null,
    todos: { todos: [] as Array<Record<string, unknown>> },
    curateItems: [] as Array<Record<string, unknown>>,
    curateDeferred: false,
    curateQueue: [] as Deferred[],
    curateCalls: [] as Array<Array<Record<string, unknown>>>,
    phase: { label: '退潮', loading: false, error: null, unavailableNote: null, limitUp: 41, limitDown: null, sealRate: 0.62, reload: () => {} },
    mainline: { top: null as unknown, loading: false, error: null },
  }
  return state
})

function deferCurate(args: unknown): Promise<unknown> {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  S.curateQueue.push({ promise, resolve, reject, args })
  return promise
}

vi.mock('@sida/base-ui', () => ({ usePausedInterval: () => {} }))
vi.mock('@sida/base-ui/components/ui/button', () => ({
  Button: ({ children, ...rest }: { children?: ReactNode } & Record<string, unknown>) => {
    const { onClick, title } = rest as { onClick?: () => void; title?: string }
    return (
      <button onClick={onClick} title={title}>
        {children}
      </button>
    )
  },
}))
vi.mock('@sida/base-ui/components/ui/status-chip', () => ({
  StatusChip: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
}))
vi.mock('@sida/biz-ui/components/KpiBand', () => ({
  default: () => <div data-testid="kpi-band" />,
  usePhaseLabel: () => S.phase,
  useMainlineTop1: () => S.mainline,
}))
vi.mock('@sida/biz-ui/components/dashboard/BreadthDistributionChart', () => ({ default: () => <div data-testid="breadth-chart" /> }))
vi.mock('@sida/biz-ui/components/dashboard/SentimentGauge', () => ({ default: () => <div /> }))
vi.mock('@sida/biz-ui/components/dashboard/FlowHistoryChart', () => ({ default: () => <div /> }))
vi.mock('@sida/biz-ui/components/dashboard/ResonancePanel', () => ({ default: () => <div /> }))
vi.mock('@sida/biz-ui/components/MarketPhaseCard', () => ({ default: () => <div /> }))
vi.mock('@sida/biz-ui/components/MarketMainlineCard', () => ({ default: () => <div /> }))
vi.mock('@sida/biz-ui/components/AnimatedNumber', () => ({ default: ({ value }: { value?: ReactNode }) => <span>{String(value)}</span> }))
vi.mock('@sida/biz-ui/components/FlashValue', () => ({ default: ({ children }: { children?: ReactNode }) => <>{children}</> }))
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
vi.mock('@sida/biz-ui/components/CaliberBadge', () => ({ default: () => <span />, caliberOf: () => 'unknown' }))
vi.mock('@sida/biz-ui/components/onboarding', () => ({ Onboarding: () => null }))
vi.mock('@/components/Skeleton', () => ({ Skeleton: () => <div /> }))
vi.mock('@/components/SkeletonRows', () => ({ default: () => <div data-testid="skeleton-rows" /> }))
vi.mock('@/components/Sparkline', () => ({ default: () => <div /> }))
vi.mock('@/components/SafeMarkdown', () => ({ default: ({ children }: { children?: ReactNode }) => <div>{children}</div> }))
vi.mock('@/components/ScanJobButton', () => ({ default: () => null }))
vi.mock('@/components/MarketStatusPills', () => ({ default: () => <div /> }))
vi.mock('@/components/BenchChart', () => ({ default: () => <div /> }))
vi.mock('@/components/BenchmarkShareCard', () => ({ default: () => null }))
vi.mock('@/components/DiagnosticsShareCard', () => ({ default: () => null }))
vi.mock('@/components/DigestShareCard', () => ({ default: () => null }))
vi.mock('@/components/StockContextMenu', () => ({ default: () => null }))
vi.mock('@/components/dashboard/IndexTrendBand', () => ({ default: () => <div data-testid="index-trend-band" /> }))
vi.mock('@/components/dashboard/MarketBroadcast', () => ({ default: () => <div data-testid="market-broadcast" /> }))
vi.mock('@/components/dashboard/CollapsibleSection', () => ({
  default: ({ header, children, id }: { header?: ReactNode; children?: ReactNode; id?: string }) => (
    <section data-testid={`evidence-${id}`}>
      <div>{header}</div>
      <div>{children}</div>
    </section>
  ),
}))
vi.mock('@/components/PageHeader', () => ({
  default: ({ title, actions, metadata }: { title?: ReactNode; actions?: ReactNode; metadata?: ReactNode }) => (
    <div>
      <div>{title}</div>
      <div>{actions}</div>
      <div>{metadata}</div>
    </div>
  ),
}))
vi.mock('@/components/DiscoveryPanel', () => ({ default: () => <div data-testid="discovery-panel" /> }))
vi.mock('@/components/DashboardCustomizer', () => ({ default: () => null }))
vi.mock('@/components/ErrorBanner', () => ({
  default: ({ errors }: { errors: Array<{ source: string; message: string }> }) => (
    <div data-testid="error-banner">{errors.map((e) => e.source).join(',')}</div>
  ),
}))
vi.mock('@/hooks/useI18n', () => {
  // t / 返回对象必须**引用稳定**: Dashboard 的 load 依赖 t, 每次渲染换新函数会让
  // useEffect([load]) 无限重跑(渲染死循环)。真实 hook 内部已 memo。
  const t = (k: string) => (k === 'dashboard.fundflowSource' ? '东财 · 两市主力' : k)
  const ctx = { locale: 'zh-CN', setLocale: () => {}, dict: {}, t }
  return { useI18n: () => ctx }
})

vi.mock('@sida/api', () => ({
  fetchAPI: () => Promise.resolve({ available: true, current: { label: '退潮', phase: 'ebb', max_height: 3, promo_rate: 0.2, seal_rate: 0.62 } }),
  dashboardApi: {
    indices: () => Promise.resolve(S.indices),
    marketCapitalFlow: () => (S.flowDeferred ? new Promise(() => {}) : Promise.resolve(S.flow)),
    anomalies: () => Promise.resolve({ items: [] }),
    intradayScan: () => (S.scanFail ? Promise.reject(new Error(S.scanFail)) : Promise.resolve(S.scan)),
    overview: () => (S.overviewFail ? Promise.reject(new Error(S.overviewFail)) : Promise.resolve(S.overview)),
    marketStatus: () => Promise.resolve([]),
    curate: (cands: Array<Record<string, unknown>>) => {
      S.curateCalls.push(cands)
      return S.curateDeferred ? deferCurate(cands) : Promise.resolve({ items: S.curateItems })
    },
  },
  portfolioApi: {
    diagnostics: () => (S.diagFail ? Promise.reject(new Error(S.diagFail)) : Promise.resolve(S.diag)),
    benchmark: () => Promise.resolve({ empty: true, reason: 'no_holdings' }),
    attribution: () => Promise.resolve({ items: [] }),
    aiReview: () => Promise.resolve({ content: '' }),
  },
  recommendationsApi: { listStrategySignals: () => Promise.resolve({ items: [] }) },
  homeApi: {
    alertHitsToday: () => (S.alertsFail ? Promise.reject(new Error(S.alertsFail)) : Promise.resolve(S.alerts)),
    todos: () => Promise.resolve(S.todos),
  },
  stocksApi: { list: () => Promise.resolve([]), create: () => Promise.resolve({}) },
  premarketApi: {
    getToday: () => Promise.resolve({ date: '2026-11-07', empty: true }),
    run: () => Promise.resolve({ queued: true }),
    getEvidence: () => Promise.resolve({ available: false, blocks: [] }),
  },
}))

import DashboardPage from '../../src/pages/Dashboard'

function renderPage() {
  return render(
    <MemoryRouter>
      <DashboardPage />
    </MemoryRouter>,
  )
}

async function flush(times = 6) {
  await act(async () => {
    for (let i = 0; i < times; i++) await Promise.resolve()
  })
}

const stock = (over: Record<string, unknown>) => ({
  symbol: '600519',
  name: '贵州茅台',
  market: 'CN',
  current_price: 1500,
  change_pct: 2.5,
  open_price: null,
  high_price: null,
  low_price: null,
  volume: null,
  turnover: null,
  alert_type: null,
  has_position: false,
  cost_price: null,
  pnl_pct: null,
  trading_style: null,
  suggestion: null,
  ...over,
})

beforeEach(() => {
  localStorage.clear()
  S.indices = []
  S.flow = {}
  S.flowDeferred = false
  S.scan = { stocks: [] }
  S.scanFail = null
  S.overview = {
    snapshot_date: '2026-11-07',
    data_freshness: {
      strategy_snapshot_date: '2026-11-07',
      entry_snapshot_date: '2026-11-07',
      market_scan_snapshot_date: '2026-11-07',
      latest_history_updated_at: '',
    },
    kpis: { watchlist_count: 3, unpriced_positions: 0 },
    action_center: { opportunities: [], risk_items: [] },
  }
  S.overviewFail = null
  S.diag = {
    position_count: 2,
    total_market_value: 100,
    hhi: 0.4,
    max_weight: 0.6,
    by_market: { CN: 100 },
    by_strategy: {},
    total_unrealized_pnl: 1,
    alerts: [],
  }
  S.diagFail = null
  S.alerts = []
  S.alertsFail = null
  S.todos = { todos: [] }
  S.curateItems = []
  S.curateDeferred = false
  S.curateQueue = []
  S.curateCalls = []
  S.phase = { label: '退潮', loading: false, error: null, unavailableNote: null, limitUp: 41, limitDown: null, sealRate: 0.62, reload: () => {} }
  S.mainline = { top: null, loading: false, error: null }
})
afterEach(() => cleanup())

describe('C. 封板率单位: 0.62 是比例, 不是 62 个百分点', () => {
  it('渲染成 62%, 不是 1% / 0.62%', async () => {
    renderPage()
    await flush()
    const conclusion = screen.getByTestId('market-conclusion')
    expect(conclusion.textContent).toContain('62%')
    expect(conclusion.textContent).not.toContain('0.62%')
  })
})

describe('D. 6 个指数都要可见', () => {
  it('后端给 6 个就渲染 6 个, 不截到 5 个', async () => {
    S.indices = ['上证指数', '深证成指', '创业板指', '科创50', '沪深300', '恒生指数'].map((name, i) => ({
      symbol: `IX${i}`,
      name,
      market: 'CN',
      current_price: 3000 + i,
      change_pct: 0.5,
      change_amount: 1,
      prev_close: 2990,
      spark: [],
    }))
    renderPage()
    await flush()
    expect(screen.getAllByTestId('index-pill')).toHaveLength(6)
    expect(screen.getByText('恒生指数')).toBeTruthy()
  })
})

describe('E. 市场全景表头: 龙头走真实嵌套 leader.name', () => {
  it('leader_name 扁平字段不存在时也能读到嵌套龙头', async () => {
    S.mainline = {
      top: { name: '光模块', limit_up_count: 9, max_boards: 4, leader: { code: '300308', name: '中际旭创', days: 3, amount: 1 } },
      loading: false,
      error: null,
    }
    renderPage()
    await flush()
    const panorama = screen.getByTestId('market-panorama')
    expect(panorama.textContent).toContain('中际旭创')
  })
})

describe('F. 资金流: 来源 + 快照时点(缺失写未知)', () => {
  it('无 timestamp 时明确写「快照时点未知」, 并标出源', async () => {
    S.flow = { total_main_flow: -12.3, total_amount: 9000, up_count: 1000, down_count: 3000, source: 'eastmoney_push2delay_cn', timestamp: null }
    renderPage()
    await flush()
    const meta = screen.getByTestId('flow-snapshot')
    expect(meta.textContent).toContain('东财 push2delay')
    expect(meta.textContent).toContain('时点未知')
  })

  it('有 timestamp 时给真实时点', async () => {
    S.flow = { total_main_flow: -12.3, total_amount: 9000, up_count: 1000, down_count: 3000, source: 'db_snapshot', timestamp: '2026-11-07T01:31:20Z' }
    renderPage()
    await flush()
    expect(screen.getByTestId('flow-snapshot').textContent).toMatch(/库内快照.*\d{2}:\d{2}:\d{2}/)
  })
})

describe('B. 今日要紧事与机会精选不重复(机会本身不删)', () => {
  it('同一标的只出现在机会精选, 不再进要紧事; 机会模块仍完整', async () => {
    S.overview = {
      ...S.overview,
      action_center: {
        opportunities: [
          {
            id: 1,
            snapshot_date: '2026-11-07',
            stock_symbol: '300750',
            stock_market: 'CN',
            stock_name: '宁德时代',
            signal: '放量突破',
            action_label: '关注',
            plan_valid: true,
          },
        ],
        risk_items: [],
      },
    }
    renderPage()
    await flush()
    const agenda = screen.getByTestId('agenda-panel')
    expect(within(agenda).queryByText('宁德时代')).toBeNull()
    const picks = screen.getByTestId('picks-panel')
    expect(within(picks).getByText('宁德时代')).toBeTruthy()
  })

  it('机会精选标出基准日', async () => {
    S.overview = {
      ...S.overview,
      action_center: {
        opportunities: [
          { id: 1, snapshot_date: '2026-11-06', stock_symbol: '300750', stock_market: 'CN', stock_name: '宁德时代', signal: 's', action_label: '关注' },
        ],
        risk_items: [],
      },
    }
    renderPage()
    await flush()
    expect(screen.getByTestId('picks-panel').textContent).toContain('2026-11-06')
  })
})

describe('A. AI 说明错配必须 fail closed, 原始候选不丢', () => {
  it('说明点名的不是这条候选的标的 → 不采用, 显示原始信号', async () => {
    S.alerts = [
      { symbol: '600519', name: '贵州茅台', market: 'CN', rule_name: '放量突破' },
      { symbol: '300750', name: '宁德时代', market: 'CN', rule_name: '放量涨停' },
    ]
    // 模型把「宁德时代」的说明贴到了第 0 条(贵州茅台)上
    S.curateItems = [
      { index: 0, importance: 90, why: '宁德时代今日放量涨停, 建议重点关注' },
      { index: 1, importance: 80, why: '宁德时代放量涨停' },
    ]
    renderPage()
    await flush()
    const agenda = screen.getByTestId('agenda-feed')
    const rows = within(agenda).getAllByTestId('agenda-row')
    // 茅台那一行只能是自己的原始信号, 不能挂别人的说明
    expect(rows[0].textContent).toContain('贵州茅台')
    expect(rows[0].textContent).toContain('触发提醒 放量突破')
    expect(agenda.textContent).not.toContain('宁德时代今日放量涨停')
  })

  it('响应乱序/属于旧快照 → 旧响应不得落地', async () => {
    S.curateDeferred = true
    S.alerts = [{ symbol: '600519', name: '贵州茅台', market: 'CN', rule_name: '放量突破' }]
    renderPage()
    await flush()
    expect(S.curateCalls.length).toBe(1)

    // 候选变化(新一轮刷新拿到另一只票) → 触发第二次 curate
    S.alerts = [{ symbol: '000001', name: '平安银行', market: 'CN', rule_name: '跌破均线' }]
    S.curateItems = [{ index: 0, importance: 80, why: '平安银行跌破年线, 短线走弱' }]
    await act(async () => {
      fireEvent.click(screen.getByText('common.refresh'))
      await Promise.resolve()
    })
    await flush()
    expect(S.curateCalls.length).toBe(2)

    // 先回来的旧响应(讲茅台) 不得落地
    await act(async () => {
      S.curateQueue[0].resolve({ items: [{ index: 0, importance: 99, why: '贵州茅台突破前高, 值得关注' }] })
      await Promise.resolve()
    })
    await flush()
    let agenda = screen.getByTestId('agenda-feed')
    expect(agenda.textContent).not.toContain('贵州茅台突破前高')

    // 新响应(讲平安银行) 落地
    await act(async () => {
      S.curateQueue[1].resolve({ items: S.curateItems })
      await Promise.resolve()
    })
    await flush()
    agenda = screen.getByTestId('agenda-feed')
    expect(agenda.textContent).toContain('平安银行跌破年线')
    expect(within(agenda).getByText('平安银行')).toBeTruthy()
  })

  it('同一标的来自两个账户(重复行) → 只留一行, 且不用 AI 说明', async () => {
    S.scan = {
      stocks: [
        stock({ symbol: '600519', name: '贵州茅台', has_position: true, suggestion: { action: 'hold', action_label: '持有', signal: '持仓浮盈 12%', should_alert: true } }),
        stock({ symbol: '600519', name: '贵州茅台', has_position: true, suggestion: { action: 'hold', action_label: '持有', signal: '持仓浮盈 3%', should_alert: true } }),
      ],
    }
    S.curateItems = [{ index: 0, importance: 90, why: '贵州茅台持仓浮盈 12%, 减仓一半' }]
    renderPage()
    await flush()
    const rows = screen.getAllByTestId('agenda-row')
    expect(rows).toHaveLength(1)
    expect(rows[0].textContent).toContain('持仓浮盈 12%')
    expect(rows[0].textContent).not.toContain('减仓一半')
  })

  it('缺 why / 越界序号 → 整份作废, 仍按原始顺序展示', async () => {
    S.alerts = [
      { symbol: '600519', name: '贵州茅台', market: 'CN', rule_name: '放量突破' },
      { symbol: '000001', name: '平安银行', market: 'CN', rule_name: '跌破均线' },
    ]
    S.curateItems = [{ index: 7, importance: 90, why: '越界' }]
    renderPage()
    await flush()
    const rows = screen.getAllByTestId('agenda-row')
    expect(rows.map((r) => r.textContent)).toEqual([expect.stringContaining('贵州茅台'), expect.stringContaining('平安银行')])
  })
})

describe('G. 请求失败不是空态', () => {
  it('scan 请求失败 → 明说失败, 不写「今日暂无明显异动」', async () => {
    S.scanFail = 'scan-down'
    renderPage()
    await flush()
    const agenda = screen.getByTestId('agenda-panel')
    expect(agenda.textContent).toContain('扫描请求失败')
    expect(agenda.textContent).not.toContain('今日暂无明显异动')
  })
})

describe('H. 成本缺失与风险项要接线', () => {
  it('unpriced_positions > 0 → 明示成本待补全', async () => {
    S.overview = { ...S.overview, kpis: { watchlist_count: 3, unpriced_positions: 2 } }
    renderPage()
    await flush()
    expect(screen.getByTestId('portfolio-unpriced').textContent).toContain('2')
  })

  it('action_center.risk_items 要展示; 请求失败时不得写「未见明显风险」', async () => {
    S.overview = {
      ...S.overview,
      action_center: {
        opportunities: [],
        risk_items: [{ id: 9, stock_symbol: '688111', stock_market: 'CN', stock_name: '金山办公', risk_level_label: '高', risk_flags: ['集中度偏高'] }],
      },
    }
    renderPage()
    await flush()
    const risk = screen.getByTestId('portfolio-risk-items')
    expect(risk.textContent).toContain('金山办公')

    cleanup()
    S.overviewFail = 'overview-down'
    renderPage()
    await flush()
    const block = screen.getByTestId('portfolio-body')
    expect(block.textContent).not.toContain('未见明显风险')
    expect(block.textContent).toContain('风险项请求失败')
  })
})
