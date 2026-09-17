// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

/**
 * 工作台三带骨架(Task 6, spec §1.2/§1.3) + 指数/板块正文接线(Task 7) + 6 标签接线(Task 17)。
 *
 * 守七件事(都是绑定条款):
 *  ① 个股(默认 `type=stock`): 带1 `HeaderBand` → 带2 主图 `KlineChart`(当日线/120 天/高 420) +
 *     右栏 `QuickRail`(外壳 `w-[320px]`) → 带3 `TabBar`(6 键, 顺序取 `WORKBENCH_TABS`)+ `TabPanel`;
 *  ② `type !== 'stock'`: 只留带1 + `IndexBoardHost`, **不渲染**主图/右栏/6 标签;
 *  ③ `type=index` → `IndexBody symbol`; `type=board` → `BoardBody code`(Task 7 接线);
 *  ④ 类型/标签切换**只改 query**(`?type=`/`?tab=`)—— 不跳页, 且保留另一个键(切类型不丢标签);
 *  ⑤ 无 symbol → 「缺少代码」兜底;
 *  ⑥ 外壳沿用既有容器惯例 `mx-auto max-w-[1500px] p-3`;
 *  ⑦ **页面级刷新**(Task 7 复审 Finding 1 + v0.6.0 遗留⑦): 带1 的刷新广播(`onRefresh`)只影响
 *     **正文子树** —— 个股分支仍用 `key={refreshKey}` 重挂载带2(`KlineChart`/`QuickRail`/激活标签
 *     都是"挂载即取数"且无 token 入参); **指数/板块分支改为传 `refreshToken={refreshKey}`** ——
 *     正文**重新取数但不重挂载**(不重放 `sida-page-enter` 入场动画、不丢正文内部 UI 状态)。
 *     带1/页面外壳**不**重挂载, 且刷新不跳页不改 query; 正文块与个股带2 同为 `mt-3` 间距(Finding 2)。
 *
 * **Task 17 追加守两件(标签接线)**:
 *  ⑧ `TabPanel` 按 `?tab=` 渲染**对应**的真实标签(六键全覆盖), 且入参 `symbol`/`market`('CN')/
 *     `hasPosition` 按页面口径传入;
 *  ⑨ **一次只挂载一个标签** —— 切标签时旧标签**卸载**、新标签**挂载**(挂载即取数 ⇒ 惰性)。
 *     这条是 spec §4.3 的核心: 六个标签各带 `InsightProvider`, 同时挂载会把六组端点一次打满。
 *
 * **Task 19 追加守一件(持仓态真源)**:
 *  ⑩ `hasPosition` 由页面自取 `GET /portfolio/summary` 判定(T19 之前的占位恒 `false` 已删):
 *     持仓在册 → `true`; 不在册 → `false`; 取数在途/失败 → `undefined`(**未知**, 绝不猜 `false`)。
 *     同时断言带1 在未知时收到 `positionUnknown=true`(显式标注「持仓态未知」)。
 *
 * 子组件(含 echarts 的 KlineChart、取数的 HeaderBand/QuickRail、Task 7 的 IndexBody/BoardBody、
 * **Task 17 的六个标签**)在本例全部 mock —— 本任务守的是**骨架与接线**, 它们的内部取数/渲染
 * 由各自单测守。`dashboardApi.portfolioSummary` 也 mock(页面唯一自取的数据) —— 用替身喂
 * **真实形状**的 `accounts[].positions[]`, 断言页面据真判定 `hasPosition`。
 */

/**
 * Task 17: 六个标签组件全部 mock。替身渲染 `data-testid="tab-<id>"` 并回显入参
 * (`symbol:market:hasPosition`), 同时用 `mocks.mount/mounted` 记"当前挂载中的标签集合"
 * —— 这是断言⑨(只挂载一个)的**直接观测点**: 真标签的取数请求都发在挂载副作用里,
 * "同时只有一个在挂载"等价于"同一时刻只有一组端点被启用"。
 */
const mocks = vi.hoisted(() => ({
  bodyFetch: vi.fn(),
  /** 遗留⑦: 正文**挂载**次数(挂载副作用只跑一次)—— 刷新后仍为 1 即证明没有重挂载。 */
  bodyMount: vi.fn(),
  mount: vi.fn(),
  unmount: vi.fn(), // 卸载计数(证明旧标签被卸载, 不是六个都留着)
  /**
   * Task 19: `GET /portfolio/summary` 替身。默认**空持仓**(页面据此判 `false`),
   * 用例可 `mockResolvedValue(...)` 喂真实形状的持仓, 或 `mockRejectedValue` 造失败。
   */
  portfolioSummary: vi.fn(),
}))

/**
 * Task 19: 页面自取持仓汇总(`dashboardApi.portfolioSummary`)。只替这一个方法, 其余导出透传
 * (页面只用到它; `importOriginal` 保真其余导出, 避免误伤)。
 */
vi.mock('@panwatch/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@panwatch/api')>()
  return {
    ...actual,
    dashboardApi: {
      ...actual.dashboardApi,
      portfolioSummary: mocks.portfolioSummary,
    },
  }
})

/** 生成一个标签替身: 渲染入参回显 + 挂载/卸载计数。 */
function makeTabMock(id: string) {
  return async () => {
    const { useEffect } = await import('react')
    function TabMock(p: { symbol: string; market: string; hasPosition?: boolean }) {
      useEffect(() => {
        mocks.mount(id)
        return () => mocks.unmount(id)
      }, [])
      return (
        <div data-testid={`tab-${id}`}>{`${id}:${p.symbol}:${p.market}:${String(p.hasPosition)}`}</div>
      )
    }
    return { default: TabMock }
  }
}

vi.mock('@/pages/workbench/tabs/L2Tab', makeTabMock('l2'))
vi.mock('@/pages/workbench/tabs/SuggestTab', makeTabMock('suggest'))
vi.mock('@/pages/workbench/tabs/FundamentalTab', makeTabMock('fundamental'))
vi.mock('@/pages/workbench/tabs/NewsTab', makeTabMock('news'))
vi.mock('@/pages/workbench/tabs/ResearchTab', makeTabMock('research'))
vi.mock('@/pages/workbench/tabs/ForecastTab', makeTabMock('forecast'))

vi.mock('@panwatch/biz-ui/components/KlineChart', () => ({
  default: (p: { symbol: string; market: string; height?: number; initialInterval?: string; initialDays?: number }) => (
    <div data-testid="kline">{`kline:${p.symbol}:${p.market}:${p.height}:${p.initialInterval}:${p.initialDays}`}</div>
  ),
}))

/**
 * Finding 1 观测点: mock 正文/主图的"取数"计数器(定义见上方 hoisted mocks)。
 * 真组件都是**挂载即取数**(挂载副作用里发请求), 故每次挂载记 1 次即可代表"一次取数请求";
 * 断言改为对比刷新前后的**调用次数差** ⇒ 直接证明刷新触发了重新取数(而非仅 DOM 变动)。
 */

// Task 7: 指数/板块正文(取数组件, 测试内 mock; 断言"类型 → 正文 + 入参"接线)
// 遗留⑦: 真组件新增了可选 `refreshToken`(页面级刷新不再靠 `key` 重挂载)——替身同形:
// 取数 effect 依赖 `[symbol, refreshToken]`(token 变即重取), 另用 `bodyMount` 记**挂载次数**
// (挂载副作用只跑一次 ⇒ 刷新后仍是 1 即证明"没有重挂载")。
vi.mock('@/pages/workbench/IndexBody', async () => {
  const { useEffect } = await import('react')
  function IndexBodyMock(p: { symbol: string; refreshToken?: number }) {
    useEffect(() => {
      mocks.bodyMount('index')
    }, [])
    // 模拟真组件"挂载/换标的/刷新 token 变化即取数"
    useEffect(() => {
      mocks.bodyFetch('index', p.symbol)
    }, [p.symbol, p.refreshToken])
    return <div data-testid="index-body">{`index-body:${p.symbol}`}</div>
  }
  return { default: IndexBodyMock }
})

vi.mock('@panwatch/biz-ui/components/workbench/BoardBody', async () => {
  const { useEffect } = await import('react')
  function BoardBodyMock(p: { code: string; refreshToken?: number }) {
    useEffect(() => {
      mocks.bodyMount('board')
    }, [])
    useEffect(() => {
      mocks.bodyFetch('board', p.code)
    }, [p.code, p.refreshToken])
    return <div data-testid="board-body">{`board-body:${p.code}`}</div>
  }
  return { default: BoardBodyMock }
})

vi.mock('@panwatch/biz-ui/components/workbench/HeaderBand', () => ({
  default: (p: {
    symbol: string
    market: string
    type: string
    onTypeChange?: (t: string) => void
    onGotoTab?: (t: string) => void
    /** Task 19: 持仓态未知标注(真实持仓源在途/失败) */
    positionUnknown?: boolean
    /** Task 7 复审 Finding 1: 页面级刷新广播(真组件里由刷新按钮点火) */
    onRefresh?: () => void
  }) => (
    <div data-testid="band1">
      <span>{`band1:${p.symbol}:${p.market}:${p.type}`}</span>
      <span data-testid="band1-position-unknown">{`unknown:${String(!!p.positionUnknown)}`}</span>
      <button type="button" onClick={() => p.onTypeChange?.('index')}>
        mock-to-index
      </button>
      <button type="button" onClick={() => p.onGotoTab?.('news')}>
        mock-goto-news
      </button>
      <button type="button" onClick={() => p.onRefresh?.()}>
        mock-refresh
      </button>
    </div>
  ),
}))

vi.mock('@panwatch/biz-ui/components/workbench/QuickRail', () => ({
  default: (p: { symbol: string; market: string }) => (
    <div data-testid="rail">{`rail:${p.symbol}:${p.market}`}</div>
  ),
}))

import StockWorkbench from '@/pages/StockWorkbench'

/** 观察点: 当前 URL 的 query(断言"只改 query, 不跳页")。 */
function SearchProbe() {
  const loc = useLocation()
  return <div data-testid="search">{loc.search}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SearchProbe />
      <Routes>
        {/* App.tsx 的真实挂载形态; `/stocks`(无 symbol)单列一条以覆盖兜底分支 */}
        <Route path="/stocks/:symbol" element={<StockWorkbench />} />
        <Route path="/stocks" element={<StockWorkbench />} />
      </Routes>
    </MemoryRouter>,
  )
}

const search = () => screen.getByTestId('search').textContent ?? ''

/** Task 19: `/portfolio/summary` 的健康返回(空持仓) —— 真实形状, 无持仓。 */
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

/** Task 19: 持仓在册的返回(单账户含 `positions[]`) —— 与 `DiscoveryPanel.holdingSet` 同口径。 */
const WITH_POSITION = {
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
  total: EMPTY_PORTFOLIO.total,
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  // 逐例清零"取数"/"挂载"计数(hoisted mock 跨例复用); 断言一律用**例内前后次数差**。
  mocks.bodyFetch.mockClear()
  mocks.bodyMount.mockClear()
  mocks.mount.mockClear()
  mocks.unmount.mockClear()
  mocks.portfolioSummary.mockReset()
})

/** Task 17 观测点: 当前**在挂载中**的标签集合(挂载过 − 已卸载过)。 */
const mountedTabs = () => {
  const ids = new Set(mocks.mount.mock.calls.map((c) => c[0] as string))
  const gone = new Set(mocks.unmount.mock.calls.map((c) => c[0] as string))
  for (const id of gone) ids.delete(id)
  return [...ids]
}

/**
 * Task 19: 逐例默认"空持仓"返回 —— 不关心持仓的用例据此看到 `hasPosition=false`(已判定未持仓,
 * 与 T19 之前的占位值同值但**语义不同**: 现在是"真查过, 不在册")。用例可在 render 前覆盖。
 */
beforeEach(() => {
  mocks.portfolioSummary.mockResolvedValue(EMPTY_PORTFOLIO)
})

/** Task 19: 等页面持仓判定落定后, 再读标签回显的 `hasPosition`(判定在 fetch 后 setState)。 */
async function expectTabHasPosition(id: string, expected: string) {
  await waitFor(() =>
    expect(screen.getByTestId(`tab-${id}`).textContent).toBe(`${id}:002636:CN:${expected}`),
  )
}

describe('StockWorkbench 三带骨架', () => {
  it('个股(默认): 带1 + 主图 + 右栏 + 6 键标签 + 真实「盘口资金」标签, 外壳沿用容器惯例', async () => {
    renderAt('/stocks/002636')

    // 带1
    expect(screen.getByTestId('band1').textContent).toContain('band1:002636:CN:stock')
    // 外壳(mx-auto max-w-[1500px] p-3 —— 与改版前同惯例)
    const root = screen.getByTestId('band1').parentElement as HTMLElement
    expect(root.className).toContain('mx-auto')
    expect(root.className).toContain('max-w-[1500px]')
    expect(root.className).toContain('p-3')

    // 带2: 主图(日线 / 120 天 / 高 420)+ 右栏固定 320px
    expect(screen.getByTestId('kline').textContent).toBe('kline:002636:CN:420:1d:120')
    expect(screen.getByTestId('rail').textContent).toBe('rail:002636:CN')
    // 主图面板外壳: `min-w-0` 是**承重**类 —— 没有它, echarts canvas 会撑破与固定
    // `w-[320px]` 右栏并排的 flex 行(overflow);`flex-1` 让它吃掉剩余宽度。
    const klineBox = screen.getByTestId('kline').parentElement as HTMLElement
    expect(klineBox.className).toContain('min-w-0')
    expect(klineBox.className).toContain('flex-1')
    expect(klineBox.className).toContain('rounded')
    expect(klineBox.className).toContain('border-border/60')
    expect(klineBox.className).toContain('p-2')
    // 右栏列固定 320px。
    // 2026-09-18: 右栏列里多了 §10.2④ 的「区间统计」卡位, rail 的**直系父**改为滚动容器,
    // 固定宽落在它上一层的**列容器**上 —— 用 closest('[class*=…]') 定位该列, 不写死层数,
    // 免得下次再插一层卡就误报(断言的是"rail 所在的列宽 320px 且不被压扁")。
    const railColumn = screen.getByTestId('rail').closest('[class*="w-[320px]"]') as HTMLElement
    expect(railColumn).toBeTruthy()
    expect(railColumn.className).toContain('shrink-0')
    // rail 自身仍在可滚动容器里(长速览卡不撑破列高)
    const railBox = screen.getByTestId('rail').parentElement as HTMLElement
    expect(railBox.className).toContain('overflow-y-auto')

    // 带3: 6 键(顺序 = WORKBENCH_TABS)+ 默认选中「盘口资金」+ **真实 L2Tab**(Task 17)
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual([
      '盘口资金',
      '建议',
      '基本面',
      '消息',
      '研究',
      '预测',
    ])
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')
    // Task 17 接线: 默认标签渲染真实组件(替身回显 symbol/market/hasPosition 的实际入参)
    // Task 19: 持仓判定在 `/portfolio/summary` 返回后落定 ⇒ 首帧为「未知」, 落定后才是 false(不在册)。
    await expectTabHasPosition('l2', 'false')
    // 判定落定(真查过 → 未持仓)后, 带1 不再显示「持仓态未知」
    await waitFor(() => expect(screen.getByTestId('band1-position-unknown').textContent).toBe('unknown:false'))
    // 其余五个**不得**挂载(惰性: 只有激活标签在挂载中)
    expect(screen.queryByTestId('tab-suggest')).toBeNull()
    expect(screen.queryByTestId('tab-fundamental')).toBeNull()
    expect(screen.queryByTestId('tab-news')).toBeNull()
    expect(screen.queryByTestId('tab-research')).toBeNull()
    expect(screen.queryByTestId('tab-forecast')).toBeNull()
    expect(mountedTabs()).toEqual(['l2'])
    // 指数/板块正文不得出现
    expect(screen.queryByTestId('index-body')).toBeNull()
    expect(screen.queryByTestId('board-body')).toBeNull()
  })

  it('?type=index: 只留带1 + IndexBody(无主图/无右栏/无 6 标签/无任何标签组件)', () => {
    renderAt('/stocks/000001?type=index')
    expect(screen.getByTestId('band1').textContent).toContain('band1:000001:CN:index')
    // Task 7 接线: 指数正文拿 symbol
    expect(screen.getByTestId('index-body').textContent).toBe('index-body:000001')
    expect(screen.queryByTestId('board-body')).toBeNull()
    expect(screen.queryByTestId('kline')).toBeNull()
    expect(screen.queryByTestId('rail')).toBeNull()
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
    // Task 17: 非个股分支**一个标签组件都不渲染**
    expect(mocks.mount).not.toHaveBeenCalled()
    expect(mountedTabs()).toEqual([])
  })

  it('?type=board: 板块正文拿 code', () => {
    renderAt('/stocks/880001?type=board')
    expect(screen.getByTestId('band1').textContent).toContain('band1:880001:CN:board')
    expect(screen.getByTestId('board-body').textContent).toBe('board-body:880001')
    expect(screen.queryByTestId('index-body')).toBeNull()
    expect(screen.queryByTestId('kline')).toBeNull()
    expect(mocks.mount).not.toHaveBeenCalled()
  })

  it('无 symbol: 「缺少代码」兜底, 不带不渲染带1', () => {
    renderAt('/stocks')
    expect(screen.getByText('缺少代码')).toBeTruthy()
    expect(screen.queryByTestId('band1')).toBeNull()
    expect(screen.queryByTestId('kline')).toBeNull()
  })

  // ---- Task 17: 六标签逐一接线 + 惰性(一次只挂载一个) ----
  it('?tab= 六键逐一: 每个 tab 渲染**对应**组件(入参 symbol/market/hasPosition 一致)', async () => {
    const cases: [string, string][] = [
      ['l2', '盘口资金'],
      ['suggest', '建议'],
      ['fundamental', '基本面'],
      ['news', '消息'],
      ['research', '研究'],
      ['forecast', '预测'],
    ]
    for (const [id, label] of cases) {
      const view = renderAt(`/stocks/002636?tab=${id}`)
      expect(screen.getByRole('tab', { name: label }).getAttribute('aria-selected')).toBe('true')
      // 激活项渲染**对应**组件。ForecastTab 不消费 symbol/market(签名同形), 故只回显 id。
      const box = screen.getByTestId(`tab-${id}`)
      if (id === 'forecast') {
        // ForecastTab 签名只收 `{ symbol, market }`(不消费; 见实现头注)⇒ hasPosition 真为 undefined
        expect(box.textContent).toBe('forecast:002636:CN:undefined')
      } else {
        // Task 19: 持仓判定落定后 = false(真查过, 不在册)
        await expectTabHasPosition(id, 'false')
      }
      // **只有它一个**在渲染中
      for (const [otherId] of cases) {
        if (otherId === id) continue
        expect(screen.queryByTestId(`tab-${otherId}`)).toBeNull()
      }
      expect(mountedTabs()).toEqual([id])
      view.unmount()
      mocks.mount.mockClear()
      mocks.unmount.mockClear()
    }
  })

  it('切标签: 写 ?tab= 且**旧标签卸载 / 新标签挂载**(一次只挂一个 —— 惰性取数的直接证据)', async () => {
    renderAt('/stocks/002636')
    expect(mountedTabs()).toEqual(['l2'])
    expect(mocks.mount).toHaveBeenCalledTimes(1)
    expect(mocks.unmount).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('tab', { name: '研究' }))

    // 只改 query, 不跳页
    expect(search()).toContain('tab=research')
    // 新标签上屏、旧标签下屏
    await expectTabHasPosition('research', 'false')
    expect(screen.queryByTestId('tab-l2')).toBeNull()
    // 旧标签**真的被卸载了**(不是六个都留着): unmount('l2') 恰一次, mount 共 2 次且第二次是 research
    expect(mocks.unmount).toHaveBeenCalledWith('l2')
    expect(mocks.mount).toHaveBeenCalledTimes(2)
    expect(mocks.mount).toHaveBeenLastCalledWith('research')
    expect(mountedTabs()).toEqual(['research'])
  })

  it('非法 ?tab= 收敛回「盘口资金」(不崩, 且渲染 l2 标签)', async () => {
    renderAt('/stocks/002636?tab=__nope__')
    await expectTabHasPosition('l2', 'false')
    expect(mountedTabs()).toEqual(['l2'])
  })

  it('带1 切类型: 写 ?type= 且**保留**已有 ?tab=(切类型不丢标签)', () => {
    renderAt('/stocks/002636?tab=news')
    fireEvent.click(screen.getByText('mock-to-index'))
    expect(search()).toContain('type=index')
    expect(search()).toContain('tab=news')
    expect(screen.getByTestId('index-body').textContent).toBe('index-body:002636')
    expect(screen.queryByTestId('kline')).toBeNull()
    // 切到指数分支后标签组件**全部卸载**(分支不同, 不再渲染)
    expect(screen.queryByTestId('tab-news')).toBeNull()
    expect(mountedTabs()).toEqual([])
  })

  it('带1 建议条跳标签(onGotoTab → ?tab=, 渲染对应标签)', async () => {
    renderAt('/stocks/002636')
    fireEvent.click(screen.getByText('mock-goto-news'))
    expect(search()).toContain('tab=news')
    await expectTabHasPosition('news', 'false')
    expect(mountedTabs()).toEqual(['news'])
  })

  // ---- Task 7 复审 Finding 1/2: 页面级刷新 + 正文块间距 ----
  // 遗留⑦ 起指数/板块分支的观测点变了: 刷新**不再**重挂载正文(旧断言是"节点身份变了"),
  // 而是把 `refreshKey` 当 `refreshToken` prop 传下去 ⇒ 断言改为**两条同时成立**:
  //  ① 正文**重新取数**(mock 的取数计数 +1 —— 真组件把 token 放进了取数 effect 的依赖);
  //  ② 正文**没有重挂载**(DOM 节点身份不变 + 挂载计数仍为 1)—— 这正是"不重放入场动画、
  //     不丢正文内部 UI 状态"的可观测等价物。
  // 个股分支仍走 `key={refreshKey}`(见实现注释): 那里的断言保持"节点换新"。
  const fetchCount = (kind: 'index' | 'board') =>
    mocks.bodyFetch.mock.calls.filter((c) => c[0] === kind).length
  const mountCount = (kind: 'index' | 'board') =>
    mocks.bodyMount.mock.calls.filter((c) => c[0] === kind).length

  it('Finding 1 + 遗留⑦ 刷新: 指数正文**重新取数**但**不重挂载**(同节点), 带1/外壳稳定不跳页', () => {
    renderAt('/stocks/000001?type=index')
    // Finding 2: 正文块与个股分支带2 同为 mt-3(不再与带1 贴死)
    const contentBox = screen.getByTestId('index-body').parentElement as HTMLElement
    expect(contentBox.className).toContain('mt-3')

    const bodyBefore = screen.getByTestId('index-body')
    const bandBefore = screen.getByTestId('band1')
    const callsBefore = fetchCount('index')
    expect(mountCount('index')).toBe(1)
    fireEvent.click(screen.getByText('mock-refresh'))

    // ① 刷新触发正文**重新取数**(token 进了取数 effect 的依赖)
    expect(fetchCount('index')).toBe(callsBefore + 1)
    expect(mocks.bodyFetch).toHaveBeenLastCalledWith('index', '000001')
    // ② **没有重挂载**: 同一 DOM 节点 + 挂载计数仍为 1(重挂载会换新节点并再记一次挂载)
    expect(screen.getByTestId('index-body')).toBe(bodyBefore)
    expect(mountCount('index')).toBe(1)
    expect(screen.getByTestId('index-body').textContent).toBe('index-body:000001')
    // 带1/外壳不重挂载, 且不跳页/不改 query
    expect(screen.getByTestId('band1')).toBe(bandBefore)
    expect(search()).toBe('?type=index')

    // 再点一次: 取数再 +1, 节点仍是同一个(可重复, 不是一次性巧合)
    fireEvent.click(screen.getByText('mock-refresh'))
    expect(fetchCount('index')).toBe(callsBefore + 2)
    expect(screen.getByTestId('index-body')).toBe(bodyBefore)
    expect(mountCount('index')).toBe(1)
  })

  it('Finding 1 + 遗留⑦ 刷新: 板块正文**重新取数**但**不重挂载** + 带 mt-3 间距', () => {
    renderAt('/stocks/880001?type=board')
    const contentBox = screen.getByTestId('board-body').parentElement as HTMLElement
    expect(contentBox.className).toContain('mt-3')

    const bodyBefore = screen.getByTestId('board-body')
    const callsBefore = fetchCount('board')
    expect(mountCount('board')).toBe(1)
    fireEvent.click(screen.getByText('mock-refresh'))

    expect(fetchCount('board')).toBe(callsBefore + 1)
    expect(mocks.bodyFetch).toHaveBeenLastCalledWith('board', '880001')
    // 不重挂载(遗留⑦)
    expect(screen.getByTestId('board-body')).toBe(bodyBefore)
    expect(mountCount('board')).toBe(1)
    expect(screen.getByTestId('board-body').textContent).toBe('board-body:880001')
  })

  it('Finding 1 刷新: 个股带2(主图 + 右栏 + 激活标签)重挂载, ?tab= 不丢', async () => {
    renderAt('/stocks/002636?tab=news')
    const klineBefore = screen.getByTestId('kline')
    const railBefore = screen.getByTestId('rail')
    // 刷新前: 标签挂载 1 次(research 从未出现), 无卸载
    expect(mocks.mount).toHaveBeenCalledTimes(1)
    expect(mocks.mount).toHaveBeenLastCalledWith('news')

    fireEvent.click(screen.getByText('mock-refresh'))

    expect(screen.getByTestId('kline')).not.toBe(klineBefore)
    expect(screen.getByTestId('rail')).not.toBe(railBefore)
    expect(screen.getByTestId('kline').textContent).toBe('kline:002636:CN:420:1d:120')
    expect(search()).toContain('tab=news')
    // Task 17: 刷新把带3(含激活标签)一起重挂载 ⇒ 标签卸载并重新挂载(= 重新取数), 且仍是 news
    await expectTabHasPosition('news', 'false')
    expect(mocks.mount).toHaveBeenCalledTimes(2)
    expect(mocks.mount).toHaveBeenLastCalledWith('news')
    expect(mocks.unmount).toHaveBeenCalledWith('news')
    // 刷新期间**没有任何别的标签**被挂载(惰性: 只 news 在列)
    expect([...new Set(mocks.mount.mock.calls.map((c) => c[0]))]).toEqual(['news'])
    // 刷新期间指数/板块正文一次都不该被取(仍是个股分支)
    expect(mocks.bodyFetch).not.toHaveBeenCalled()
  })

  // ---- Task 19: `hasPosition` 真源(三态; 绝不把"未知"说成"未持仓") ----
  it('持仓在册: 页面据 `/portfolio/summary` 判 hasPosition=true, 带1 不再标「持仓态未知」', async () => {
    mocks.portfolioSummary.mockResolvedValue(WITH_POSITION)
    renderAt('/stocks/002636?tab=fundamental')

    await expectTabHasPosition('fundamental', 'true')
    // 已判定 ⇒ 未知标注消失
    expect(screen.getByTestId('band1-position-unknown').textContent).toBe('unknown:false')
    // 真源只打一次(页面挂载时取其自消费的持仓汇总)
    expect(mocks.portfolioSummary).toHaveBeenCalledTimes(1)
    expect(mocks.portfolioSummary).toHaveBeenCalledWith({ include_quotes: false })
  })

  it('不持仓(在册但无该标的): 判 false, 且**不是**"未知"', async () => {
    // 持仓里有 002636 之外的票 ⇒ 当前标的判 false(与"未知"区分)
    mocks.portfolioSummary.mockResolvedValue({
      ...WITH_POSITION,
      accounts: [
        { ...WITH_POSITION.accounts[0], positions: [{ ...WITH_POSITION.accounts[0].positions[0], symbol: '600519' }] },
      ],
    })
    renderAt('/stocks/002636?tab=l2')
    await expectTabHasPosition('l2', 'false')
    expect(screen.getByTestId('band1-position-unknown').textContent).toBe('unknown:false')
  })

  it('取数失败: hasPosition 保持**未知**(undefined), 带1 显式标「持仓态未知」—— 不猜 false', async () => {
    mocks.portfolioSummary.mockRejectedValue(new Error('boom'))
    renderAt('/stocks/002636?tab=l2')

    // 失败 ⇒ 一直是未知; 标签拿到 `undefined`, 带1 的未知标注为 true
    await waitFor(() => expect(screen.getByTestId('band1-position-unknown').textContent).toBe('unknown:true'))
    expect(screen.getByTestId('tab-l2').textContent).toBe('l2:002636:CN:undefined')
  })

  it('未知时**不**伪装成 false 的时序: 首帧(< fetch 落定)即为 undefined + 未知标注', () => {
    // 永不落定的 fetch ⇒ 停在"未知"; 断言首帧不是 false 假象
    mocks.portfolioSummary.mockReturnValue(new Promise(() => {}))
    renderAt('/stocks/002636?tab=suggest')
    expect(screen.getByTestId('tab-suggest').textContent).toBe('suggest:002636:CN:undefined')
    expect(screen.getByTestId('band1-position-unknown').textContent).toBe('unknown:true')
  })

  // ---- Task 19 Finding 3: 指数/板块**不**发 /portfolio/summary(结果无人消费) ----
  it('?type=index: **不**打 `/portfolio/summary`(持仓态对其无用, 不发无用请求)', async () => {
    renderAt('/stocks/000001?type=index')
    // 指数正文已渲染(证明组件挂载完成、副作用已跑过一轮)
    expect(screen.getByTestId('index-body').textContent).toBe('index-body:000001')
    // 等一拍(若 hook 会发请求, 此刻已发出)再断言"零调用"
    await new Promise((r) => setTimeout(r, 0))
    expect(mocks.portfolioSummary).not.toHaveBeenCalled()
  })

  it('?type=board: **不**打 `/portfolio/summary`', async () => {
    renderAt('/stocks/880001?type=board')
    expect(screen.getByTestId('board-body').textContent).toBe('board-body:880001')
    await new Promise((r) => setTimeout(r, 0))
    expect(mocks.portfolioSummary).not.toHaveBeenCalled()
  })

  it('个股(对照): **打** `/portfolio/summary` 一次(三态语义不受闸门影响)', async () => {
    mocks.portfolioSummary.mockResolvedValue(WITH_POSITION)
    renderAt('/stocks/002636?tab=l2')
    await expectTabHasPosition('l2', 'true')
    expect(mocks.portfolioSummary).toHaveBeenCalledTimes(1)
    expect(mocks.portfolioSummary).toHaveBeenCalledWith({ include_quotes: false })
  })

  it('stock → index 切类型: 切走后不再(重复)请求持仓; 判定只在个股分支发生', async () => {
    renderAt('/stocks/002636')
    await expectTabHasPosition('l2', 'false')
    expect(mocks.portfolioSummary).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByText('mock-to-index'))
    expect(screen.getByTestId('index-body').textContent).toBe('index-body:002636')
    await new Promise((r) => setTimeout(r, 0))
    // 闸门关闭 ⇒ 不因切类型再发一次(仍恰 1 次)
    expect(mocks.portfolioSummary).toHaveBeenCalledTimes(1)
  })
})
