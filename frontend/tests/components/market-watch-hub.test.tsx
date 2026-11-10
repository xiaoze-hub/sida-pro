// @vitest-environment jsdom
//
// 大盘观察工作区(MarketWatchHub, 2026-11-07) —— 定向交互测试。
//
// 钉六件事:
//  ① 默认视图 = 指数(只渲染指数页, 不请求 ETF 接口, 不同屏拼两页);
//  ② ETF 是独立视图: 点页签切换后指数页卸载、ETF 页取数渲染, URL 写 view 且保留其余参数;
//  ③ 深链/兼容: `?view=` 直开; 旧 `?tab=` 规范化成 view; 非法 view 回退默认并纠正 URL;
//  ④ 来源/口径双标注: 两视图 source 与 caliber 常驻同屏, 显式声明不合并/不换算;
//     ETF 视图明确「不提供单只净申购、不代表单只 ETF 身份」(不为身份背书);
//  ⑤ 旧页面保留: 「单独打开」指向当前视图的原路由(/indexes、/etf-watch);
//  ⑥ 权限与现状一致: 无 `view_quotes` 时指数视图隐藏、ETF 仍可达(今天 /etf-watch 无守卫);
//     myPerms=null 不过滤。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, useLocation } from 'react-router-dom'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
}))

import MarketWatchHub from '@/components/market-watch/MarketWatchHub'

// 2026-11-07: 指数视图内嵌上证指数 KlineChart 主图, 其容器尺寸自适应用 ResizeObserver
// (jsdom 无此 API) → 桩掉, 只补浏览器能力, 不改任何断言语义。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub

const OVERVIEW = {
  items: [
    {
      symbol: '000001',
      name: '上证指数',
      trade_date: '20261106',
      close: 3421.52,
      pct_chg: 0.82,
      amount: 5.2e11,
      up_home: 1802,
      down_home: 520,
      outside: 2.1e11,
      inside: 1.9e11,
      xl_net: 1.2e9,
      lg_net: 8e8,
      md_net: -3e8,
      sm_net: -1.7e9,
      main_net: 2e9,
      net_all: 0,
      active_buy: 1.5e9,
      active_sell: 1.1e9,
      quality: 'ok',
      caliber: 'tdx_index_agg',
    },
  ],
  missing: [],
  complete: true,
  stale: false,
  as_of: '2026-11-06 15:00:00',
  caliber: 'tdx_index_agg',
}

const HISTORY = {
  items: [
    { trade_date: '20261105', close: 3393.1, pct_chg: 0.31, amount: 4.9e11, main_net: 5e8, net_all: 1e8 },
  ],
}

const INTRADAY = {
  date: '20261106',
  phase: 'closed',
  session_phase: 'closed',
  session_time: '15:00',
  stale: false,
  state: 'normal',
  series: [
    { minute: '0930', amount_yi: 12.3, cum_yi: 12.3, baseline_cum_yi: 10.1, complete: true },
    { minute: '0931', amount_yi: 9.8, cum_yi: 22.1, baseline_cum_yi: 19.5, complete: true },
  ],
  totals: {
    cum_yi: 22.1,
    baseline_avg_yi: 19.5,
    ratio_vs_baseline: 1.13,
    baseline_days: 5,
    baseline_days_requested: 20,
    last_minute: '0931',
    etfs_expected: 20,
    full_minutes: 2,
    minutes: 2,
  },
}

const SUPPORT = {
  state: 'ok',
  as_of: '20261106',
  stale: false,
  unit: '亿份',
  net_sub_unit: '亿份',
  scale_unit: '亿份',
  threshold_share_yi: 100,
  threshold_yi: 100,
  series: [{ date: '20261106', scale_yi: 26800, net_sub_yi: 120.5 }],
  support_days: [{ date: '20261106', net_sub_yi: 120.5 }],
  support_count: 1,
  sample_count: 40,
  latest: { date: '20261106', scale_yi: 26800, net_sub_yi: 120.5 },
  latest_percentile_in_support_days: 100,
  single_share_supported: false,
  single_share_note: '单只 ETF 份额为当前值回填, 不提供单只净申购。',
}

const DAILY = {
  state: 'ok',
  dates: ['20261105', '20261106'],
  total_amount_yi: [420.5, 510.2],
  net_sub_yi: [30.1, 120.5],
  by_category: [{ category: '沪深300', amount_yi: [180.2, 240.9] }],
}

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location">{`${loc.pathname}${loc.search}${loc.hash}`}</div>
}

function renderHub(options?: {
  initialEntries?: string[]
  myPerms?: Set<string> | null
  initialView?: 'index' | 'etf'
}) {
  const { initialEntries = ['/market-watch'], myPerms = new Set(['view_quotes']), initialView } = options ?? {}
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <MarketWatchHub myPerms={myPerms} initialView={initialView} />
      <LocationProbe />
    </MemoryRouter>,
  )
}

/** 已发出的请求路径(便于断言「没去请求另一视图」) */
function calls(match: string) {
  return mocks.fetchAPI.mock.calls.map((c) => String(c[0])).filter((u) => u.includes(match))
}

beforeEach(() => {
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockImplementation((url: string) => {
    const u = String(url)
    if (u.includes('/indexes/overview')) return Promise.resolve(OVERVIEW)
    if (u.includes('/indexes/') && u.includes('/history')) return Promise.resolve(HISTORY)
    if (u.includes('/etf-watch/intraday')) return Promise.resolve(INTRADAY)
    if (u.includes('/etf-watch/support')) return Promise.resolve(SUPPORT)
    if (u.includes('/etf-watch/daily')) return Promise.resolve(DAILY)
    return Promise.resolve({})
  })
})

afterEach(() => {
  cleanup()
  localStorage.clear()
})

describe('MarketWatchHub 大盘观察工作区', () => {
  it.each([
    [120.5, '+120.50亿'],
    [-12.34, '-12.34亿'],
    [0, '0.00亿'],
    [null, '--'],
  ])('最新市场级份额变动 %s 的数值和单位不重复', async (amount, expected) => {
    mocks.fetchAPI.mockImplementation((url: string) => {
      if (url.includes('/etf-watch/support')) return Promise.resolve({ ...SUPPORT, latest: { ...SUPPORT.latest, net_sub_yi: amount } })
      if (url.includes('/etf-watch/intraday')) return Promise.resolve(INTRADAY)
      if (url.includes('/etf-watch/daily')) return Promise.resolve(DAILY)
      return Promise.resolve({})
    })
    renderHub({ initialEntries: ['/market-watch?view=etf'] })
    const label = await screen.findByText('最新交易日份额变动')
    await waitFor(() => expect(label.parentElement?.textContent).toContain('20261106'))
    const value = label.nextElementSibling?.textContent
    expect(value).toBe(expected)
    expect(screen.getAllByText(/市场级只有 SC8 份额/).length).toBeGreaterThan(0)
  })
  it('默认「指数全景」: 只渲染指数页, 不请求 ETF 接口, 页签与 URL 一致', async () => {
    renderHub({ initialEntries: ['/market-watch?window=30'] })

    expect(await screen.findByText('盘面指数')).toBeTruthy()
    expect(screen.getByTestId('market-watch-hub').dataset.view).toBe('index')
    expect(screen.getByRole('tab', { name: '指数全景' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('tab', { name: 'ETF 观察' }).getAttribute('aria-selected')).toBe('false')
    // 不同屏合并: ETF 页既没取数也没渲染
    expect(calls('/etf-watch/')).toHaveLength(0)
    expect(screen.queryByText('国家队 ETF')).toBeNull()
    expect(screen.queryByText('盘中分钟观测')).toBeNull()
    expect(calls('/indexes/overview').length).toBeGreaterThan(0)
    // 深链参数不动
    expect(screen.getByTestId('location').textContent).toContain('window=30')
  })

  it('点页签切到「ETF 观察」: 指数页卸载、ETF 取数渲染, URL 写 view 且保留其余参数', async () => {
    renderHub({ initialEntries: ['/market-watch?window=30'] })
    await screen.findByText('盘面指数')

    fireEvent.click(screen.getByRole('tab', { name: 'ETF 观察' }))

    expect(await screen.findByText('国家队 ETF')).toBeTruthy()
    await waitFor(() => expect(calls('/etf-watch/intraday').length).toBeGreaterThan(0))
    // 真实切换(不是 hidden 同屏): 指数页已卸载, 两页数字不混排
    expect(screen.queryByText('盘面指数')).toBeNull()
    expect(screen.getByTestId('market-watch-hub').dataset.view).toBe('etf')
    const loc = screen.getByTestId('location').textContent ?? ''
    expect(loc).toContain('view=etf')
    expect(loc).toContain('window=30')
  })

  it('深链 ?view=etf 直接打开 ETF 视图, 不触发指数接口', async () => {
    renderHub({ initialEntries: ['/market-watch?view=etf'] })

    expect(await screen.findByText('国家队 ETF')).toBeTruthy()
    expect(calls('/indexes/')).toHaveLength(0)
    expect(screen.getByRole('tab', { name: 'ETF 观察' }).getAttribute('aria-selected')).toBe('true')
  })

  it('旧 ?tab=etf 写法规范化为 ?view=etf, 并保留其他查询参数', async () => {
    renderHub({ initialEntries: ['/market-watch?tab=etf&days=40'] })

    expect(await screen.findByText('国家队 ETF')).toBeTruthy()
    const loc = screen.getByTestId('location').textContent ?? ''
    expect(loc).toContain('view=etf')
    expect(loc).not.toContain('tab=')
    expect(loc).toContain('days=40')
  })

  it('非法 view 回退默认指数视图并纠正 URL(replace, 不留 ?view=nope)', async () => {
    renderHub({ initialEntries: ['/market-watch?view=nope'] })

    expect(await screen.findByText('盘面指数')).toBeTruthy()
    await waitFor(() => {
      const loc = screen.getByTestId('location').textContent ?? ''
      expect(loc).toContain('view=index')
      expect(loc).not.toContain('nope')
    })
  })

  it('两视图来源与口径常驻同屏, 显式声明不合并/不换算, ETF 不背书单只身份', async () => {
    renderHub()
    await screen.findByText('盘面指数')

    const bar = screen.getByTestId('market-watch-caliber-bar')
    const index = screen.getByTestId('market-watch-caliber-index')
    const etf = screen.getByTestId('market-watch-caliber-etf')

    expect(index.textContent).toContain('tdx_index_agg')
    expect(index.textContent).toContain('不可换算')
    expect(etf.textContent).toContain('SC8')
    expect(etf.textContent).toContain('不提供单只净申购')
    expect(etf.textContent).toContain('不代表任何单只 ETF 的身份')
    expect(bar.textContent).toContain('不做合并、换算或互相补数')
    // 当前面板只有指数页, 没有把 ETF 数字并进来
    expect(screen.getByTestId('market-watch-view-panel').dataset.view).toBe('index')
    expect(screen.queryByText('盘中分钟观测')).toBeNull()
  })

  it('「单独打开」指向当前视图的原路由, 老页面 /indexes、/etf-watch 不被破坏', async () => {
    renderHub()
    await screen.findByText('盘面指数')
    expect(screen.getByTestId('market-watch-legacy-link').getAttribute('href')).toBe('/indexes')

    fireEvent.click(screen.getByRole('tab', { name: 'ETF 观察' }))
    await screen.findByText('国家队 ETF')
    expect(screen.getByTestId('market-watch-legacy-link').getAttribute('href')).toBe('/etf-watch')
  })

  it('无 view_quotes 权限: 指数视图隐藏, ETF 视图仍可达(与 /etf-watch 无守卫的现状一致)', async () => {
    renderHub({ myPerms: new Set<string>() })

    expect(await screen.findByText('国家队 ETF')).toBeTruthy()
    expect(screen.getAllByRole('tab')).toHaveLength(1)
    expect(screen.getByRole('tab', { name: 'ETF 观察' })).toBeTruthy()
    expect(screen.getByTestId('market-watch-hub').dataset.view).toBe('etf')
    expect(screen.queryByTestId('market-watch-hub-denied')).toBeNull()
    expect(calls('/indexes/')).toHaveLength(0)
  })

  // 安全复审 2026-11-07: 旧实现 myPerms=null 时 viewAllowed() 一律放行(fail OPEN) ——
  // 权限还没读到/读失败时, 受 view_quotes 守卫的指数视图照常渲染, 与 App.tsx PermGuard
  // 2026-09-15 结论(未加载=fail closed)相反。下面两条钉死 fail closed。
  it('myPerms=null(权限未加载/拉取失败): fail closed —— 不渲染受守卫视图, 也不发起取数', async () => {
    renderHub({ myPerms: null })

    expect(await screen.findByTestId('market-watch-hub-perm-pending')).toBeTruthy()
    // 受 view_quotes 守卫的指数页一行都不许出现, 接口一次都不许打
    expect(screen.queryByText('盘面指数')).toBeNull()
    expect(calls('/indexes/')).toHaveLength(0)
    expect(calls('/etf-watch/')).toHaveLength(0)
    // 未确权前不猜视图, 页签也不出
    expect(screen.queryByTestId('market-watch-hub')).toBeNull()
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
  })

  it('深链 ?view=index + myPerms=null 也不放行(不能靠 URL 绕过未确权)', async () => {
    renderHub({ initialEntries: ['/market-watch?view=index'], myPerms: null })

    expect(await screen.findByTestId('market-watch-hub-perm-pending')).toBeTruthy()
    expect(screen.queryByText('盘面指数')).toBeNull()
    expect(calls('/indexes/')).toHaveLength(0)
  })
})
