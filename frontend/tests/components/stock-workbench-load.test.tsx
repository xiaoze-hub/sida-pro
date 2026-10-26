// @vitest-environment jsdom
//
// perf(2026-10-05) 回归钉: 个股工作台(`/stocks/:symbol`, StockWorkbench)的**加载链**。
//
// 背景(B1 生产走查 2026-10-01): `/stocks/002361` 冷态 settle ~22.7s。页面侧可动的只有加载链:
//  1. K 线主图**首帧即挂载** —— 不因页内任何子请求(持仓汇总 / 数据源健康 / 各卡取数)而延后;
//  2. 无关状态变更(右栏折叠、切标签等)不再牵连主图整棵重渲染 —— 主图内部的多条重绘 effect
//     (marker/价格线/资金柱)依赖 props 身份, 每帧新数组会反复重跑;
//  3. 派生数据(成本线数组)稳定引用: 持仓 60s 轮询换对象但成本价不变时, 主图不重渲染。
//
// 本测试不依赖真实网络(全部 mock)。真 KlineChart 会建 lightweight-charts 实例(jsdom 无 canvas),
// 故在此以**计数替身**替换 —— 它守的是**页面接线与重渲染边界**, 图表内部加载链由
// `kline-load-chain.test.tsx` 用假图表库直接渲染真组件来守。
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const H = vi.hoisted(() => ({
  portfolio: vi.fn(),
  klineRenders: [] as Array<Record<string, unknown>>,
}))

vi.mock('@panwatch/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@panwatch/api')>()
  return {
    ...actual,
    fetchAPI: vi.fn(() => Promise.resolve({})),
    insightApi: {
      quote: vi.fn(() => Promise.resolve({})),
      moreInfo: vi.fn(() => Promise.resolve({})),
      klineSummary: vi.fn(() => Promise.resolve(null)),
    },
    dashboardApi: {
      ...actual.dashboardApi,
      portfolioSummary: (...args: unknown[]) => H.portfolio(...args),
    },
  }
})

// 六个标签/右栏/指数板块正文/带1/数据源健康全部替身 —— 本文件只钉页面接线与重渲染边界。
vi.mock('@/pages/workbench/tabs/L2Tab', () => ({ default: ({ symbol }: { symbol: string }) => <div data-testid="tab-l2">{symbol}</div> }))
vi.mock('@/pages/workbench/tabs/SuggestTab', () => ({ default: () => <div data-testid="tab-suggest" /> }))
vi.mock('@/pages/workbench/tabs/FundamentalTab', () => ({ default: () => <div data-testid="tab-fundamental" /> }))
vi.mock('@/pages/workbench/tabs/NewsTab', () => ({ default: () => <div data-testid="tab-news" /> }))
vi.mock('@/pages/workbench/tabs/ResearchTab', () => ({ default: () => <div data-testid="tab-research" /> }))
vi.mock('@/pages/workbench/tabs/ForecastTab', () => ({ default: () => <div data-testid="tab-forecast" /> }))
vi.mock('@/pages/workbench/IndexBody', () => ({ default: () => <div data-testid="index-body" /> }))
vi.mock('@panwatch/biz-ui/components/workbench/BoardBody', () => ({ default: () => <div data-testid="board-body" /> }))

vi.mock('@panwatch/biz-ui/components/KlineChart', () => ({
  default: (props: Record<string, unknown>) => {
    H.klineRenders.push(props)
    // costLines 未传 = 一条成本线都不画(页面在无持仓/未知时**不传**, 禁猜/禁代填)
    return <div data-testid="kline" data-cost={props.costLines === undefined ? 'none' : 'cost'} />
  },
}))

vi.mock('@panwatch/biz-ui/components/workbench/QuickRail', () => ({
  default: () => <div data-testid="rail" />,
}))

vi.mock('@panwatch/biz-ui/components/workbench/HeaderBand', () => ({
  default: (p: { positionUnknown?: boolean }) => (
    <div data-testid="band1">
      <span data-testid="band1-position-unknown">{`unknown:${String(!!p.positionUnknown)}`}</span>
    </div>
  ),
}))

// 数据源健康替身: 返回**跨渲染稳定**的 isReady/reasonOf(定位无关重渲染的前提)。
const isReady = () => true
const reasonOf = () => ''
vi.mock('@/hooks/useSourceHealth', () => ({
  useSourceHealth: () => ({ health: {}, loading: false, isReady, reasonOf }),
}))

import StockWorkbench from '@/pages/StockWorkbench'

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/stocks/:symbol" element={<StockWorkbench />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** Task 19 形状的空持仓返回(页面据此判 hasPosition=false)。 */
const EMPTY_PORTFOLIO = {
  accounts: [],
  total: {
    total_market_value: 0,
    total_cost: 0,
    total_pnl: 0,
    total_pnl_pct: 0,
    total_daily_pnl: 0,
    daily_pnl_period: 'unknown' as const,
    daily_pnl_label: '',
    daily_pnl_date: null,
    available_funds: 0,
    total_assets: 0,
  },
}

beforeEach(() => {
  H.klineRenders.length = 0
  H.portfolio.mockReset()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const klineRenderCount = () => H.klineRenders.length

describe('StockWorkbench 加载链(perf 回归)', () => {
  it('K线主图首帧即挂载: 持仓汇总仍在途时主图已在渲染, 且不传假成本线', () => {
    // 持仓请求永挂起 ⇒ 页面处于"持仓态未知"
    H.portfolio.mockReturnValue(new Promise(() => {}))
    renderAt('/stocks/002636')

    // 主图已在(同步首帧), 不因持仓/其它子请求延后
    expect(screen.getByTestId('kline')).toBeTruthy()
    expect(klineRenderCount()).toBe(1)
    // 未知态: 显式标注, 且主图未收到成本线(不猜、不代填)
    expect(screen.getByTestId('band1-position-unknown').textContent).toBe('unknown:true')
    expect(screen.getByTestId('kline').getAttribute('data-cost')).toBe('none')
  })

  it('无关状态变更(右栏折叠 / 切标签)不重渲染已 memo 的主图', async () => {
    H.portfolio.mockResolvedValue(EMPTY_PORTFOLIO)
    renderAt('/stocks/002636')

    // 等持仓判定落定(此后页面状态稳定)
    await waitFor(() =>
      expect(screen.getByTestId('band1-position-unknown').textContent).toBe('unknown:false'),
    )
    const before = klineRenderCount()

    // ① 右栏展开 → 收起(两次 setState, 页面重渲染)
    fireEvent.click(screen.getByTestId('rail-toggle'))
    expect(screen.getByTestId('rail')).toBeTruthy()
    fireEvent.click(screen.getByTestId('rail-toggle'))
    expect(screen.queryByTestId('rail')).toBeNull()
    expect(klineRenderCount()).toBe(before)

    // ② 切标签(改 ?tab= → 页面重渲染; 主图 props 未变 ⇒ 不重渲染)
    fireEvent.click(screen.getByRole('tab', { name: '研究' }))
    await act(async () => {})
    expect(screen.getByTestId('tab-research')).toBeTruthy()
    expect(klineRenderCount()).toBe(before)
  })

  it('派生成本线引用稳定: 持仓轮询换对象但成本价不变 ⇒ 主图不重渲染', async () => {
    // 第一拍: 该标的在册, 成本价 10
    const POS = {
      ...EMPTY_PORTFOLIO,
      accounts: [
        {
          id: 1,
          name: '默认',
          available_funds: 0,
          total_cost: 0,
          total_market_value: 0,
          total_pnl: 0,
          total_pnl_pct: 0,
          total_daily_pnl: 0,
          daily_pnl_period: 'unknown' as const,
          daily_pnl_label: '',
          daily_pnl_date: null,
          total_assets: 0,
          positions: [
            {
              id: 1,
              stock_id: 1,
              symbol: '002636',
              name: '金安国纪',
              market: 'CN',
              cost_price: 10,
              quantity: 100,
              invested_amount: 1000,
              trading_style: 'swing',
              current_price: 12,
              change_pct: 1.2,
            },
          ],
        },
      ],
    }
    H.portfolio.mockResolvedValue(POS)
    renderAt('/stocks/002636')

    // 成本线到达后主图收到成本价
    await waitFor(() => expect(screen.getByTestId('kline').getAttribute('data-cost')).toBe('cost'))
    const afterCost = klineRenderCount()
    expect(afterCost).toBe(2) // 挂载(1) + 成本线到达(2)

    // 无关重渲染(切标签)不改成本价 ⇒ 引用稳定, 主图不再重渲染
    fireEvent.click(screen.getByRole('tab', { name: '研究' }))
    await act(async () => {})
    expect(klineRenderCount()).toBe(afterCost)
  })
})
