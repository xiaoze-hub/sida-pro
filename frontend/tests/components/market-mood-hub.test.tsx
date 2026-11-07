// @vitest-environment jsdom
//
// 市场情绪工作区(MarketMoodHub, 2026-11-07) —— 定向交互测试。
//
// 钉五件事:
//  ① 分视图: 默认「题材情绪」, 点页签切到「连板晋级树」, 只渲染当前视图(不做同屏合并);
//  ② 来源/口径双标注: 两视图的 source 与 caliber 常驻同屏, 并显式声明不合并/不换算;
//  ③ 深链: `?view=` 可直接打开对应视图; 切换时保留其余查询参数; 旧 `?tab=` 规范化成 view;
//     非法 view 回退默认并纠正 URL(不留历史垃圾);
//  ④ 老深链: 「单独打开」始终指向当前视图的原路由(/theme-mood、/replay-tree);
//  ⑤ 权限: 无 `view_quotes` → 显式无可用视图; myPerms=null(未加载) 不过滤。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, useLocation } from 'react-router-dom'

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  tree: vi.fn(),
  series: vi.fn(),
}))

vi.mock('@sida/api', () => ({
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
  marketReplayApi: {
    tree: (...a: unknown[]) => mocks.tree(...a),
    series: (...a: unknown[]) => mocks.series(...a),
  },
}))

import MarketMoodHub from '@/components/market-mood/MarketMoodHub'

const BOARD = {
  trade_date: '20261106',
  window: 20,
  count: 1,
  dates: ['20261105', '20261106'],
  market: [{ date: '20261106', score: 71.7 }],
  items: [
    {
      block_code: '881101.SH',
      block_name: '元件',
      block_type: 'industry',
      score: 78.2,
      delta: 4.1,
      confidence: 86,
      core: true,
      s1: 80, s2: 75, s3: 82, s4: 70, s5: 60,
      limit_up_cnt: 9, max_boards: 3,
      core_stocks: [],
      cells: [
        { date: '20261105', score: 74.1, limit_up_cnt: 7 },
        { date: '20261106', score: 78.2, limit_up_cnt: 9 },
      ],
    },
  ],
}

const LADDER = {
  dates: ['20261106'],
  ladder: [],
  mode: 'finalized',
  stale: false,
  degraded: null,
  live_day: null,
}

const TREE = {
  available: true,
  trade_date: '20261106',
  trade_date_iso: '2026-11-06',
  market: 'CN',
  source: 'eastmoney_replay',
  caliber: 'eastmoney_zt',
  quality: 'ok',
  missing_dims: [],
  limit_up_count: 82,
  broken_count: 12,
  break_rate_pct: 12.77,
  seal_rate_pct: 87.23,
  limit_down_count: 2,
  limit_down_max_days: 2,
  max_streak: 6,
  first_board_count: 78,
  ge2_count: 3,
  ge3_count: 3,
  ladder_available: false,
  levels: [],
  detail_returned: 0,
  detail_partial: false,
  detail_note: null,
  promotion: {
    available: false,
    promo_rate_pct: null,
    sample_count: null,
    sample_note: '样本 = 上一交易日东财涨停池家数(同口径同池)',
    baseline_available: false,
    baseline_note: '表内未存同池无条件基准(口径缺口)',
    excludes_one_word: false,
    one_word_note: '未剔除 T+1 一字涨停(口径缺口)',
    one_word_count: 3,
    per_level_available: false,
    per_level_note: '分层晋级率(首板→2板→…)表内未存逐层晋级明细, 不做前端推算',
  },
  prev_comparison: { available: false },
  phase: 'main_up',
  phase_label: '主升',
  emotion_available: true,
  emotion_basis_days: 12,
  cross_check: {
    eastmoney_zt: 82,
    tq_pool: 86,
    tq_max_streak: 6,
    diff: 4,
    note: '差北交所',
    tq_available: true,
  },
  seal_mv_ratio: null,
  seal_mv_ratio_note: '封单额/流通市值比表内未存, 不做换算编造; 逐股封单额见成员明细',
  updated_at: '2026-11-06T15:52:00',
}

const SERIES = {
  caliber: 'eastmoney_zt',
  caliber_note: '东财三池同源同日(不含北交所); 与 TQ 全A自算口径并存不混算',
  days: 10,
  count: 0,
  items: [],
  note: null,
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{`${loc.pathname}${loc.search}${loc.hash}`}</div>
}

function renderHub(options?: {
  initialEntries?: string[]
  myPerms?: Set<string> | null
  initialView?: 'theme-mood' | 'replay-tree'
}) {
  const { initialEntries = ['/market-mood'], myPerms = new Set(['view_quotes']), initialView } = options ?? {}
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <MarketMoodHub myPerms={myPerms} initialView={initialView} />
      <LocationProbe />
    </MemoryRouter>,
  )
}

function boardCalls() {
  return mocks.fetchAPI.mock.calls.map((c) => String(c[0])).filter((u) => u.includes('/theme-mood/board'))
}

beforeEach(() => {
  mocks.fetchAPI.mockReset()
  mocks.tree.mockReset()
  mocks.series.mockReset()
  mocks.fetchAPI.mockImplementation((url: string) => {
    const u = String(url)
    if (u.includes('/theme-mood/board')) return Promise.resolve(BOARD)
    if (u.includes('/theme-mood/ladder')) return Promise.resolve(LADDER)
    if (u.includes('/market-data/breadth-history')) return Promise.resolve({ ok: false, items: [] })
    if (u.includes('/tdx/etf-track')) return Promise.resolve({ total: 0, etfs: [] })
    return Promise.resolve({})
  })
  mocks.tree.mockResolvedValue(TREE)
  mocks.series.mockResolvedValue(SERIES)
})

afterEach(() => {
  cleanup()
  localStorage.clear()
})

describe('MarketMoodHub 分视图工作区', () => {
  it('默认「题材情绪」: 只渲染题材页, 不请求晋级树, 页签状态与 URL 一致', async () => {
    renderHub({ initialEntries: ['/market-mood?window=30'] })

    expect((await screen.findAllByText('元件')).length).toBeGreaterThan(0)
    expect(screen.getByTestId('market-mood-hub').dataset.view).toBe('theme-mood')
    expect(screen.getByRole('tab', { name: '题材情绪' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('tab', { name: '连板晋级树' }).getAttribute('aria-selected')).toBe('false')
    // 工作区没有把两页拼在一起: 晋级树未取数、也未渲染
    expect(mocks.tree).not.toHaveBeenCalled()
    expect(screen.queryByTestId('replay-tree')).toBeNull()
    expect(boardCalls().length).toBeGreaterThan(0)
    // 深链参数不动
    expect(screen.getByTestId('location').textContent).toContain('window=30')
  })

  it('点页签切到「连板晋级树」: 题材页卸载、晋级树取数渲染, URL 写 view 且保留其余参数', async () => {
    renderHub({ initialEntries: ['/market-mood?window=30'] })
    await screen.findAllByText('元件')

    fireEvent.click(screen.getByRole('tab', { name: '连板晋级树' }))

    expect(await screen.findByTestId('replay-tree')).toBeTruthy()
    await waitFor(() => expect(mocks.tree).toHaveBeenCalled())
    // 真实视图切换: 题材页已卸载(不是 hidden 同屏), 数字不混排
    expect(screen.queryAllByText('元件')).toHaveLength(0)
    expect(screen.getByTestId('market-mood-hub').dataset.view).toBe('replay-tree')
    const loc = screen.getByTestId('location').textContent ?? ''
    expect(loc).toContain('view=replay-tree')
    expect(loc).toContain('window=30')
  })

  it('深链 ?view=replay-tree 直接打开晋级树, 不触发题材页取数', async () => {
    renderHub({ initialEntries: ['/market-mood?view=replay-tree'] })

    expect(await screen.findByTestId('replay-tree')).toBeTruthy()
    expect(boardCalls()).toHaveLength(0)
    expect(screen.getByRole('tab', { name: '连板晋级树' }).getAttribute('aria-selected')).toBe('true')
  })

  it('旧 ?tab= 写法规范化为 ?view=, 并保留其他查询参数', async () => {
    renderHub({ initialEntries: ['/market-mood?tab=replay-tree&window=30'] })

    expect(await screen.findByTestId('replay-tree')).toBeTruthy()
    const loc = screen.getByTestId('location').textContent ?? ''
    expect(loc).toContain('view=replay-tree')
    expect(loc).not.toContain('tab=')
    expect(loc).toContain('window=30')
  })

  it('非法 view 回退默认视图并纠正 URL(replace, 不留 ?view=nope)', async () => {
    renderHub({ initialEntries: ['/market-mood?view=nope'] })

    expect((await screen.findAllByText('元件')).length).toBeGreaterThan(0)
    await waitFor(() => {
      const loc = screen.getByTestId('location').textContent ?? ''
      expect(loc).toContain('view=theme-mood')
      expect(loc).not.toContain('nope')
    })
  })

  it('两种来源与口径常驻同屏标注, 并显式声明不合并/不换算', async () => {
    renderHub()
    await screen.findAllByText('元件')

    const bar = screen.getByTestId('market-mood-caliber-bar')
    const theme = screen.getByTestId('market-mood-caliber-theme-mood')
    const replay = screen.getByTestId('market-mood-caliber-replay-tree')
    // 来源(接口/表)各自标注
    expect(theme.textContent).toContain('theme_mood_daily')
    expect(theme.textContent).toContain('收盘确认口径')
    expect(replay.textContent).toContain('market_replay_daily')
    expect(replay.textContent).toContain('eastmoney_zt')
    expect(replay.textContent).toContain('tq_pool')
    // 边界声明
    expect(bar.textContent).toContain('不做合并、换算或互相补数')
    // 当前面板只有题材页, 没有把晋级树数字并进来
    expect(screen.getByTestId('market-mood-view-panel').dataset.view).toBe('theme-mood')
  })

  it('「单独打开」指向当前视图的原路由, 老深链 /theme-mood、/replay-tree 不被破坏', async () => {
    renderHub()
    await screen.findAllByText('元件')
    expect(screen.getByTestId('market-mood-legacy-link').getAttribute('href')).toBe('/theme-mood')

    fireEvent.click(screen.getByRole('tab', { name: '连板晋级树' }))
    await screen.findByTestId('replay-tree')
    expect(screen.getByTestId('market-mood-legacy-link').getAttribute('href')).toBe('/replay-tree')
  })

  it('无 view_quotes 权限: 显式无可用视图, 不渲染任何数据页', async () => {
    renderHub({ myPerms: new Set<string>() })

    expect(await screen.findByTestId('market-mood-hub-denied')).toBeTruthy()
    expect(screen.queryByRole('tab')).toBeNull()
    expect(screen.queryByTestId('market-mood-view-panel')).toBeNull()
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
    expect(mocks.tree).not.toHaveBeenCalled()
  })

  it('myPerms=null(权限未加载)时不闪隐: 两视图可见', async () => {
    renderHub({ myPerms: null })

    expect((await screen.findAllByText('元件')).length).toBeGreaterThan(0)
    expect(screen.getAllByRole('tab')).toHaveLength(2)
  })
})
