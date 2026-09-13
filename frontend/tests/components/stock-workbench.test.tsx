// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

/**
 * 工作台三带骨架(Task 6, spec §1.2/§1.3)。
 *
 * 守五件事(都是本任务的绑定条款):
 *  ① 个股(默认 `type=stock`): 带1 `HeaderBand` → 带2 主图 `KlineChart`(当日线/120 天/高 420) +
 *     右栏 `QuickRail`(外壳 `w-[320px]`) → 带3 `TabBar`(6 键, 顺序取 `WORKBENCH_TABS`)+ `TabPanel`;
 *  ② `type !== 'stock'`: 只留带1 + `IndexBoardHost`, **不渲染**主图/右栏/6 标签;
 *  ③ 类型/标签切换**只改 query**(`?type=`/`?tab=`)—— 不跳页, 且保留另一个键(切类型不丢标签);
 *  ④ 无 symbol → 「缺少代码」兜底;
 *  ⑤ 外壳沿用既有容器惯例 `mx-auto max-w-[1500px] p-3`。
 *
 * 三个子组件(含 echarts 的 KlineChart、取数的 HeaderBand/QuickRail)在本例全部 mock ——
 * 本任务守的是**骨架与接线**, 它们的内部取数/渲染由各自单测守。真数据: 本页零取数。
 */
vi.mock('@panwatch/biz-ui/components/KlineChart', () => ({
  default: (p: { symbol: string; market: string; height?: number; initialInterval?: string; initialDays?: number }) => (
    <div data-testid="kline">{`kline:${p.symbol}:${p.market}:${p.height}:${p.initialInterval}:${p.initialDays}`}</div>
  ),
}))

vi.mock('@panwatch/biz-ui/components/workbench/HeaderBand', () => ({
  default: (p: {
    symbol: string
    market: string
    type: string
    onTypeChange?: (t: string) => void
    onGotoTab?: (t: string) => void
  }) => (
    <div data-testid="band1">
      <span>{`band1:${p.symbol}:${p.market}:${p.type}`}</span>
      <button type="button" onClick={() => p.onTypeChange?.('index')}>
        mock-to-index
      </button>
      <button type="button" onClick={() => p.onGotoTab?.('news')}>
        mock-goto-news
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
    expect(screen.queryByText(/正文建设中/)).toBeNull()
  })

  it('?type=index: 只留带1 + IndexBoardHost(无主图/无右栏/无 6 标签)', () => {
    renderAt('/stocks/000001?type=index')
    expect(screen.getByTestId('band1').textContent).toContain('band1:000001:CN:index')
    expect(screen.getByText(/指数正文建设中/)).toBeTruthy()
    expect(screen.queryByTestId('kline')).toBeNull()
    expect(screen.queryByTestId('rail')).toBeNull()
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
  })

  it('?type=board: 板块正文占位', () => {
    renderAt('/stocks/880001?type=board')
    expect(screen.getByTestId('band1').textContent).toContain('band1:880001:CN:board')
    expect(screen.getByText(/板块正文建设中/)).toBeTruthy()
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
    expect(screen.getByText(/指数正文建设中/)).toBeTruthy()
    expect(screen.queryByTestId('kline')).toBeNull()
  })

  it('带1 建议条跳标签(onGotoTab → ?tab=)', () => {
    renderAt('/stocks/002636')
    fireEvent.click(screen.getByText('mock-goto-news'))
    expect(search()).toContain('tab=news')
    expect(screen.getByText(/「消息」建设中/)).toBeTruthy()
  })
})
