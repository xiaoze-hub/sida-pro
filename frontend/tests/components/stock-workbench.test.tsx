// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

/**
 * 工作台三带骨架(Task 6, spec §1.2/§1.3) + 指数/板块正文接线(Task 7)。
 *
 * 守六件事(都是绑定条款):
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
 * 四个子组件(含 echarts 的 KlineChart、取数的 HeaderBand/QuickRail、Task 7 的 IndexBody/BoardBody)
 * 在本例全部 mock —— 本任务守的是**骨架与接线**, 它们的内部取数/渲染由各自单测守。真数据: 本页零取数。
 */
vi.mock('@panwatch/biz-ui/components/KlineChart', () => ({
  default: (p: { symbol: string; market: string; height?: number; initialInterval?: string; initialDays?: number }) => (
    <div data-testid="kline">{`kline:${p.symbol}:${p.market}:${p.height}:${p.initialInterval}:${p.initialDays}`}</div>
  ),
}))

/**
 * Finding 1 观测点: mock 正文/主图的"取数"计数器。
 * 真组件都是**挂载即取数**(挂载副作用里发请求), 故每次挂载记 1 次即可代表"一次取数请求";
 * 断言改为对比刷新前后的**调用次数差** ⇒ 直接证明刷新触发了重新取数(而非仅 DOM 变动)。
 */
const mocks = vi.hoisted(() => ({ bodyFetch: vi.fn() }))

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
  // 逐例清零"取数"计数(hoisted mock 跨例复用); 断言一律用**例内前后次数差**。
  mocks.bodyFetch.mockClear()
})

describe('StockWorkbench 三带骨架', () => {
  it('个股(默认): 带1 + 主图 + 右栏 + 6 键标签 + 面板占位, 外壳沿用容器惯例', () => {
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

    // 带3: 6 键(顺序 = WORKBENCH_TABS)+ 默认选中「盘口资金」+ 面板占位
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
    expect(screen.getByText(/「盘口资金」建设中/)).toBeTruthy()
    // 指数/板块正文不得出现
    expect(screen.queryByTestId('index-body')).toBeNull()
    expect(screen.queryByTestId('board-body')).toBeNull()
  })

  it('?type=index: 只留带1 + IndexBody(无主图/无右栏/无 6 标签)', () => {
    renderAt('/stocks/000001?type=index')
    expect(screen.getByTestId('band1').textContent).toContain('band1:000001:CN:index')
    // Task 7 接线: 指数正文拿 symbol
    expect(screen.getByTestId('index-body').textContent).toBe('index-body:000001')
    expect(screen.queryByTestId('board-body')).toBeNull()
    expect(screen.queryByTestId('kline')).toBeNull()
    expect(screen.queryByTestId('rail')).toBeNull()
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
  })

  it('?type=board: 板块正文拿 code', () => {
    renderAt('/stocks/880001?type=board')
    expect(screen.getByTestId('band1').textContent).toContain('band1:880001:CN:board')
    expect(screen.getByTestId('board-body').textContent).toBe('board-body:880001')
    expect(screen.queryByTestId('index-body')).toBeNull()
    expect(screen.queryByTestId('kline')).toBeNull()
  })

  it('无 symbol: 「缺少代码」兜底, 不带不渲染带1', () => {
    renderAt('/stocks')
    expect(screen.getByText('缺少代码')).toBeTruthy()
    expect(screen.queryByTestId('band1')).toBeNull()
    expect(screen.queryByTestId('kline')).toBeNull()
  })

  it('?tab=news 深链: 「消息」选中并渲染其占位', () => {
    renderAt('/stocks/002636?tab=news')
    const news = screen.getByRole('tab', { name: '消息' })
    expect(news.getAttribute('aria-selected')).toBe('true')
    expect(screen.getByText(/「消息」建设中/)).toBeTruthy()
  })

  it('非法 ?tab= 收敛回「盘口资金」(不崩)', () => {
    renderAt('/stocks/002636?tab=__nope__')
    expect(screen.getByText(/「盘口资金」建设中/)).toBeTruthy()
  })

  it('点标签: 只写 ?tab=, 不跳页', () => {
    renderAt('/stocks/002636')
    fireEvent.click(screen.getByRole('tab', { name: '研究' }))
    expect(search()).toContain('tab=research')
    expect(screen.getByText(/「研究」建设中/)).toBeTruthy()
  })

  it('带1 切类型: 写 ?type= 且**保留**已有 ?tab=(切类型不丢标签)', () => {
    renderAt('/stocks/002636?tab=news')
    fireEvent.click(screen.getByText('mock-to-index'))
    expect(search()).toContain('type=index')
    expect(search()).toContain('tab=news')
    expect(screen.getByTestId('index-body').textContent).toBe('index-body:002636')
    expect(screen.queryByTestId('kline')).toBeNull()
  })

  it('带1 建议条跳标签(onGotoTab → ?tab=)', () => {
    renderAt('/stocks/002636')
    fireEvent.click(screen.getByText('mock-goto-news'))
    expect(search()).toContain('tab=news')
    expect(screen.getByText(/「消息」建设中/)).toBeTruthy()
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

  it('Finding 1 刷新: 个股带2(主图 + 右栏)重挂载, ?tab= 不丢', () => {
    renderAt('/stocks/002636?tab=news')
    const klineBefore = screen.getByTestId('kline')
    const railBefore = screen.getByTestId('rail')
    fireEvent.click(screen.getByText('mock-refresh'))

    expect(screen.getByTestId('kline')).not.toBe(klineBefore)
    expect(screen.getByTestId('rail')).not.toBe(railBefore)
    expect(screen.getByTestId('kline').textContent).toBe('kline:002636:CN:420:1d:120')
    expect(search()).toContain('tab=news')
    expect(screen.getByText(/「消息」建设中/)).toBeTruthy()
    // 刷新期间指数/板块正文一次都不该被取(仍是个股分支)
    expect(mocks.bodyFetch).not.toHaveBeenCalled()
  })
})
