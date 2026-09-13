// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
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
 *  ⑦ **页面级刷新**(Task 7 复审 Finding 1): 带1 的刷新广播(`onRefresh`)只重挂载**正文子树**
 *     —— 指数/板块正文与个股带2(`KlineChart`/`QuickRail`)整棵换新(挂载副作用 ⇒ 重新取数),
 *     带1/页面外壳**不**重挂载, 且刷新不跳页不改 query; 正文块与个股带2 同为 `mt-3` 间距(Finding 2)。
 *
 * **Task 17 追加守两件(标签接线)**:
 *  ⑧ `TabPanel` 按 `?tab=` 渲染**对应**的真实标签(六键全覆盖), 且入参 `symbol`/`market`('CN')/
 *     `hasPosition` 按页面口径传入(hasPosition 现为占位 `false` —— Task 6 Ruling, 见实现头注);
 *  ⑨ **一次只挂载一个标签** —— 切标签时旧标签**卸载**、新标签**挂载**(挂载即取数 ⇒ 惰性)。
 *     这条是 spec §4.3 的核心: 六个标签各带 `InsightProvider`, 同时挂载会把六组端点一次打满。
 *
 * 子组件(含 echarts 的 KlineChart、取数的 HeaderBand/QuickRail、Task 7 的 IndexBody/BoardBody、
 * **Task 17 的六个标签**)在本例全部 mock —— 本任务守的是**骨架与接线**, 它们的内部取数/渲染
 * 由各自单测守。真数据: 本页零取数。
 */

/**
 * Task 17: 六个标签组件全部 mock。替身渲染 `data-testid="tab-<id>"` 并回显入参
 * (`symbol:market:hasPosition`), 同时用 `mocks.mount/mounted` 记"当前挂载中的标签集合"
 * —— 这是断言⑨(只挂载一个)的**直接观测点**: 真标签的取数请求都发在挂载副作用里,
 * "同时只有一个在挂载"等价于"同一时刻只有一组端点被启用"。
 */
const mocks = vi.hoisted(() => ({
  bodyFetch: vi.fn(),
  mount: vi.fn(),
  unmount: vi.fn(), // 卸载计数(证明旧标签被卸载, 不是六个都留着)
}))

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
vi.mock('@/pages/workbench/IndexBody', async () => {
  const { useEffect } = await import('react')
  function IndexBodyMock(p: { symbol: string }) {
    // 模拟真组件"挂载/换标的即取数"
    useEffect(() => {
      mocks.bodyFetch('index', p.symbol)
    }, [p.symbol])
    return <div data-testid="index-body">{`index-body:${p.symbol}`}</div>
  }
  return { default: IndexBodyMock }
})

vi.mock('@panwatch/biz-ui/components/workbench/BoardBody', async () => {
  const { useEffect } = await import('react')
  function BoardBodyMock(p: { code: string }) {
    useEffect(() => {
      mocks.bodyFetch('board', p.code)
    }, [p.code])
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
    /** Task 7 复审 Finding 1: 页面级刷新广播(真组件里由刷新按钮点火) */
    onRefresh?: () => void
  }) => (
    <div data-testid="band1">
      <span>{`band1:${p.symbol}:${p.market}:${p.type}`}</span>
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

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  // 逐例清零"取数"/"挂载"计数(hoisted mock 跨例复用); 断言一律用**例内前后次数差**。
  mocks.bodyFetch.mockClear()
  mocks.mount.mockClear()
  mocks.unmount.mockClear()
})

/** Task 17 观测点: 当前**在挂载中**的标签集合(挂载过 − 已卸载过)。 */
const mountedTabs = () => {
  const ids = new Set(mocks.mount.mock.calls.map((c) => c[0] as string))
  const gone = new Set(mocks.unmount.mock.calls.map((c) => c[0] as string))
  for (const id of gone) ids.delete(id)
  return [...ids]
}

describe('StockWorkbench 三带骨架', () => {
  it('个股(默认): 带1 + 主图 + 右栏 + 6 键标签 + 真实「盘口资金」标签, 外壳沿用容器惯例', () => {
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
    const railBox = screen.getByTestId('rail').parentElement as HTMLElement
    expect(railBox.className).toContain('w-[320px]')
    expect(railBox.className).toContain('shrink-0')

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
    expect(screen.getByTestId('tab-l2').textContent).toBe('l2:002636:CN:false')
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
  it('?tab= 六键逐一: 每个 tab 渲染**对应**组件(入参 symbol/market/hasPosition 一致)', () => {
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
        expect(box.textContent).toBe(`${id}:002636:CN:false`)
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

  it('切标签: 写 ?tab= 且**旧标签卸载 / 新标签挂载**(一次只挂一个 —— 惰性取数的直接证据)', () => {
    renderAt('/stocks/002636')
    expect(mountedTabs()).toEqual(['l2'])
    expect(mocks.mount).toHaveBeenCalledTimes(1)
    expect(mocks.unmount).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('tab', { name: '研究' }))

    // 只改 query, 不跳页
    expect(search()).toContain('tab=research')
    // 新标签上屏、旧标签下屏
    expect(screen.getByTestId('tab-research').textContent).toBe('research:002636:CN:false')
    expect(screen.queryByTestId('tab-l2')).toBeNull()
    // 旧标签**真的被卸载了**(不是六个都留着): unmount('l2') 恰一次, mount 共 2 次且第二次是 research
    expect(mocks.unmount).toHaveBeenCalledWith('l2')
    expect(mocks.mount).toHaveBeenCalledTimes(2)
    expect(mocks.mount).toHaveBeenLastCalledWith('research')
    expect(mountedTabs()).toEqual(['research'])
  })

  it('非法 ?tab= 收敛回「盘口资金」(不崩, 且渲染 l2 标签)', () => {
    renderAt('/stocks/002636?tab=__nope__')
    expect(screen.getByTestId('tab-l2').textContent).toBe('l2:002636:CN:false')
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

  it('带1 建议条跳标签(onGotoTab → ?tab=, 渲染对应标签)', () => {
    renderAt('/stocks/002636')
    fireEvent.click(screen.getByText('mock-goto-news'))
    expect(search()).toContain('tab=news')
    expect(screen.getByTestId('tab-news').textContent).toBe('news:002636:CN:false')
    expect(mountedTabs()).toEqual(['news'])
  })

  // ---- Task 7 复审 Finding 1/2: 页面级刷新 + 正文块间距 ----
  // 两层观测: ① mock 正文的"取数"调用次数(每次挂载 +1 —— 真组件挂载即取数)必须**增加**;
  // ② DOM 节点身份 —— React 在 `key` 变化时卸载并重建该子树 ⇒ 节点换新。带1/外壳未挂 key,
  //    必须保持**同一节点**(吸顶带不丢焦点/滚动)。
  const fetchCount = (kind: 'index' | 'board') =>
    mocks.bodyFetch.mock.calls.filter((c) => c[0] === kind).length

  it('Finding 1 刷新: 指数正文重新取数(调用次数 +1)且重挂载, 带1/外壳稳定不跳页', () => {
    renderAt('/stocks/000001?type=index')
    // Finding 2: 正文块与个股分支带2 同为 mt-3(不再与带1 贴死)
    const contentBox = screen.getByTestId('index-body').parentElement as HTMLElement
    expect(contentBox.className).toContain('mt-3')

    const bodyBefore = screen.getByTestId('index-body')
    const bandBefore = screen.getByTestId('band1')
    const callsBefore = fetchCount('index')
    fireEvent.click(screen.getByText('mock-refresh'))

    // ① 刷新触发正文**重新取数**
    expect(fetchCount('index')).toBe(callsBefore + 1)
    expect(mocks.bodyFetch).toHaveBeenLastCalledWith('index', '000001')
    // ② 正文重挂载(内容不变), 带1/外壳不重挂载, 且不跳页/不改 query
    expect(screen.getByTestId('index-body')).not.toBe(bodyBefore)
    expect(screen.getByTestId('index-body').textContent).toBe('index-body:000001')
    expect(screen.getByTestId('band1')).toBe(bandBefore)
    expect(search()).toBe('?type=index')
  })

  it('Finding 1 刷新: 板块正文重新取数(调用次数 +1)+ 带 mt-3 间距', () => {
    renderAt('/stocks/880001?type=board')
    const contentBox = screen.getByTestId('board-body').parentElement as HTMLElement
    expect(contentBox.className).toContain('mt-3')

    const bodyBefore = screen.getByTestId('board-body')
    const callsBefore = fetchCount('board')
    fireEvent.click(screen.getByText('mock-refresh'))

    expect(fetchCount('board')).toBe(callsBefore + 1)
    expect(mocks.bodyFetch).toHaveBeenLastCalledWith('board', '880001')
    expect(screen.getByTestId('board-body')).not.toBe(bodyBefore)
    expect(screen.getByTestId('board-body').textContent).toBe('board-body:880001')
  })

  it('Finding 1 刷新: 个股带2(主图 + 右栏 + 激活标签)重挂载, ?tab= 不丢', () => {
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
    expect(screen.getByTestId('tab-news').textContent).toBe('news:002636:CN:false')
    expect(mocks.mount).toHaveBeenCalledTimes(2)
    expect(mocks.mount).toHaveBeenLastCalledWith('news')
    expect(mocks.unmount).toHaveBeenCalledWith('news')
    // 刷新期间**没有任何别的标签**被挂载(惰性: 只 news 在列)
    expect([...new Set(mocks.mount.mock.calls.map((c) => c[0]))]).toEqual(['news'])
    // 刷新期间指数/板块正文一次都不该被取(仍是个股分支)
    expect(mocks.bodyFetch).not.toHaveBeenCalled()
  })
})
