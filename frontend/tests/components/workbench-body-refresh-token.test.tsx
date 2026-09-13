// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

/**
 * 遗留⑦: 指数/板块正文的 `refreshToken` —— **真组件**守卫。
 *
 * 页面测试(`stock-workbench.test.tsx`)把两个正文都 mock 掉了, 它只能证明"页面把 refreshKey 当
 * prop 传下去了"; **真组件是否把 token 放进了取数 effect 的依赖**(即刷新是否真的重新取数)必须由
 * 本文件守 —— 否则漏加依赖, 页面测试照样全绿而线上"点刷新没反应"。
 *
 * 三件事(两个正文同形):
 *  ① 挂载取一次数;
 *  ② `refreshToken` 变化 ⇒ **再取一次**(且**不重挂载**: 根节点 DOM 身份不变 —— 重挂载会重放
 *     `sida-page-enter` 入场动画并丢掉正文自己的内部状态, 这正是遗留⑦ 要消掉的);
 *  ③ token 不变的重渲染 ⇒ **不多取**(依赖数组没写错成"每次渲染都取"); 换标的 ⇒ 仍按 symbol 重取。
 *
 * 真数据纪律: mock 的是**网络层**(`@panwatch/api` 的 `fetchAPI`), 组件取数/渲染走真实代码;
 * `InteractiveKline`(echarts 主图)与本守卫无关, 用替身避免把图表库拉进 jsdom。
 */

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))

// IndexBody 的 K线主图与本守卫无关(它自己另有取数), 用替身避免拉入 echarts。
vi.mock('@panwatch/biz-ui/components/InteractiveKline', () => ({
  default: (p: { symbol: string }) => <div data-testid="kline-stub">{`kline:${p.symbol}`}</div>,
}))

import IndexBody from '@/pages/workbench/IndexBody'
import BoardBody from '@panwatch/biz-ui/components/workbench/BoardBody'

/** 指数详情夹具(形状对齐 `src/web/api/market.py` 的 /market/indices/{symbol} 消费方声明)。 */
const INDEX_DETAIL = {
  symbol: '000001',
  name: '上证指数',
  market: 'CN',
  quote: {
    current_price: 3888.11,
    change_pct: 0.42,
    change_amount: 16.2,
    prev_close: 3871.91,
    open: 3875.0,
    high: 3895.5,
    low: 3866.1,
    volume: 345_678_900,
    amount: 456_789_000_000,
  },
  klines: [],
  amount_trend: [],
}

/** 板块详情夹具(形状对齐 `src/web/api/boards.py` 的 /boards/{code} 消费方声明)。 */
const BOARD_DETAIL = {
  block_code: '880001',
  name: '半导体',
  board_type: 'industry',
  source: 'tdx',
  today: { date: '20260913', change_pct: 1.25, fund_net: 1_234_567_890, volume: 98_765_432_100 },
  has_daily: true,
  live: true,
}

/** 按 URL 分流; 未预期的 URL 直接抛(测试失败要响)。 */
function respond(url: unknown) {
  const u = String(url)
  if (u.includes('/market/indices/')) return INDEX_DETAIL
  if (u.includes('/market-data/market-capital-flow')) return { total_main_flow: 12.3, up_count: 10, down_count: 5 }
  if (u.includes('/constituents')) return { count: 0, source: 'tdx', items: [] }
  if (u.includes('/boards/rotation')) return { days: 5, items: [] }
  if (u.includes('/boards/')) return BOARD_DETAIL
  throw new Error(`unexpected url ${u}`)
}

beforeEach(() => {
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockImplementation(async (url: unknown) => respond(url))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

/** 某端点前缀的取数次数(本守卫的唯一"真的重取了吗"观测点)。 */
const calls = (frag: string) => mocks.fetchAPI.mock.calls.filter((c) => String(c[0]).includes(frag)).length

/**
 * **精确 URL** 的取数次数 —— `/boards/880001` 是 `/boards/880001/constituents` 的子串,
 * 用 `includes` 会把两条数成一条端点的两次(计数失真), 故板块详情走精确匹配。
 */
const exactCalls = (url: string) => mocks.fetchAPI.mock.calls.filter((c) => String(c[0]) === url).length

describe('IndexBody refreshToken(遗留⑦, 真组件)', () => {
  it('挂载取一次; token 变化再取一次且**不重挂载**; token 不变不多取', async () => {
    const { container, rerender } = render(<IndexBody symbol="000001" refreshToken={0} />)
    // 数据落定锚点用指数**现价值**: 正文不渲染指数名(名称/标题已并入带1 HeaderBand)
    await waitFor(() => expect(screen.getByText('3888.11')).toBeTruthy())
    expect(calls('/market/indices/')).toBe(1)

    // 根节点(`sida-page-enter` 那层)的 DOM 身份: 重挂载会换新节点并重放入场动画
    const rootBefore = container.firstElementChild
    expect(rootBefore?.className).toContain('sida-page-enter')

    // token 不变的重渲染(新 element, 同值)⇒ 不多取
    // (注: 挂载时不传 token、之后传 0 是**换值** ⇒ 会重取, 那不是本例要守的语义)
    rerender(<IndexBody symbol="000001" refreshToken={0} />)
    await waitFor(() => expect(screen.getByText('3888.11')).toBeTruthy())
    expect(calls('/market/indices/')).toBe(1)

    // token 变化 ⇒ 重新取数(大盘资金流那条也一起重取), 但节点不换
    rerender(<IndexBody symbol="000001" refreshToken={1} />)
    await waitFor(() => expect(calls('/market/indices/')).toBe(2))
    expect(container.firstElementChild).toBe(rootBefore)
    expect(screen.getByText('3888.11')).toBeTruthy()

    // 再刷一次: 仍累加, 仍同一节点
    rerender(<IndexBody symbol="000001" refreshToken={2} />)
    await waitFor(() => expect(calls('/market/indices/')).toBe(3))
    expect(container.firstElementChild).toBe(rootBefore)
  })

  it('不传 refreshToken(向后兼容): 只在挂载/换标的时取数, 普通重渲染不多取', async () => {
    const { rerender } = render(<IndexBody symbol="000001" />)
    await waitFor(() => expect(calls('/market/indices/')).toBe(1))
    rerender(<IndexBody symbol="000001" />)
    await waitFor(() => expect(screen.getByText('3888.11')).toBeTruthy())
    expect(calls('/market/indices/')).toBe(1)
  })

  it('换标的仍按 symbol 重取(token 语义不覆盖换股)', async () => {
    const { rerender } = render(<IndexBody symbol="000001" refreshToken={1} />)
    await waitFor(() => expect(calls('/market/indices/')).toBe(1))
    rerender(<IndexBody symbol="399001" refreshToken={1} />)
    await waitFor(() => expect(calls('/market/indices/')).toBe(2))
    expect(mocks.fetchAPI.mock.calls.some((c) => String(c[0]).includes('/market/indices/399001'))).toBe(true)
  })

  it('过期请求后到**不得**覆盖新数据(复审 Finding 2: `seqRef` 取号守卫)', async () => {
    // 第 1 次(挂载)取数**挂住不发**; token 变化触发第 2 次并先回好数据; 随后让第 1 次**失败后到**。
    // 没有取号守卫时那次过期失败会 `setError`, 而本组件渲染顺序是 loading → **error → data**
    // (error 优先) ⇒ 已到手的好内容会被整块换成错误横幅。去掉守卫本用例必红。
    let rejectStale: (e: unknown) => void = () => {}
    let n = 0
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      const u = String(url)
      if (!u.includes('/market/indices/')) return respond(url)
      n += 1
      if (n === 1) return new Promise((_res, rej) => { rejectStale = rej })
      return { ...INDEX_DETAIL, quote: { ...INDEX_DETAIL.quote, current_price: 3999.99 } }
    })

    const { rerender } = render(<IndexBody symbol="000001" refreshToken={0} />)
    await waitFor(() => expect(calls('/market/indices/')).toBe(1))
    rerender(<IndexBody symbol="000001" refreshToken={1} />)
    await waitFor(() => expect(screen.getByText('3999.99')).toBeTruthy())

    // 过期那次现在才失败
    rejectStale(new Error('HTTP 503 过期请求'))
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })

    // 好数据仍在屏(⇒ 没被 error 分支顶掉); 且界面没卡在"加载中"
    // (过期号不许清 loading —— 但最新那次已经清过了)
    expect(screen.getByText('3999.99')).toBeTruthy()
    expect(screen.queryByText('加载中...')).toBeNull()
  })

  it('取数必须带 cacheMode:"reload"(生产走查缺陷: 默认 30s GET 缓存会吞掉手动刷新)', async () => {
    // `fetchAPI` 默认有 30s GET 缓存(`packages/api/src/client.ts:67`)。不传 reload 时:
    // 点带1「刷新」→ `refreshToken` 变 → `load()` **确实重跑**(上面的用例已证), 但拿到的是
    // **缓存响应** ⇒ 30s 内刷新等于什么都没发生。生产实测: TTL 内点击网络计数 0, 过 TTL 后计数 1。
    // 兄弟组件 `BoardBody` 早就传了 reload, 两个分支不该不一致 —— 故在此钉住。
    render(<IndexBody symbol="000001" refreshToken={0} />)
    await waitFor(() => expect(calls('/market/indices/')).toBe(1))
    const opt = (frag: string) =>
      mocks.fetchAPI.mock.calls.find((c) => String(c[0]).includes(frag))?.[1] as
        | { cacheMode?: string }
        | undefined
    expect(opt('/market/indices/')?.cacheMode).toBe('reload')
    // 附属的大盘资金流同样要 reload(否则刷新拿缓存)
    await waitFor(() => expect(calls('market-capital-flow')).toBe(1))
    expect(opt('market-capital-flow')?.cacheMode).toBe('reload')
  })
})

describe('BoardBody refreshToken(遗留⑦, 真组件)', () => {
  function renderBoard(props: { code: string; refreshToken?: number }) {
    return render(
      <MemoryRouter>
        <BoardBody {...props} />
      </MemoryRouter>,
    )
  }

  it('挂载取一次; token 变化再取一次且**不重挂载**; token 不变不多取', async () => {
    const { container, rerender } = renderBoard({ code: '880001', refreshToken: 0 })
    // 数据落定锚点用「今日涨跌幅」值: 正文不渲染板块名(名称/标题已并入带1 HeaderBand)
    await waitFor(() => expect(screen.getByText('+1.25%')).toBeTruthy())
    expect(exactCalls('/boards/880001')).toBe(1)

    const rootBefore = container.firstElementChild
    expect(rootBefore?.className).toContain('sida-page-enter')

    // token 不变(新 element, 同值)⇒ 不多取
    rerender(
      <MemoryRouter>
        <BoardBody code="880001" refreshToken={0} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByText('+1.25%')).toBeTruthy())
    expect(exactCalls('/boards/880001')).toBe(1)

    rerender(
      <MemoryRouter>
        <BoardBody code="880001" refreshToken={1} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(exactCalls('/boards/880001')).toBe(2))
    expect(container.firstElementChild).toBe(rootBefore)
    // 成分股/轮动两条附属取数也随 token 重跑(同一个 load 里)
    expect(calls('/constituents')).toBe(2)
    expect(calls('/boards/rotation')).toBe(2)
  })

  it('过期请求后到**不得**冒出错误横幅(复审 Finding 2: `seqRef` 取号守卫)', async () => {
    // 与 IndexBody 同型, 但本组件的 `error` 是**额外**横幅(`{error && <div>…}`)而非顶掉正文,
    // 故这里的关键断言是"**过期错误横幅不出现**"(去掉守卫则该横幅会挂在好内容上方)。
    let rejectStale: (e: unknown) => void = () => {}
    let n = 0
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      const u = String(url)
      if (u === '/boards/880001') {
        n += 1
        if (n === 1) return new Promise((_res, rej) => { rejectStale = rej })
        return { ...BOARD_DETAIL, today: { ...BOARD_DETAIL.today, change_pct: 2.5 } }
      }
      return respond(url)
    })

    const { rerender } = renderBoard({ code: '880001', refreshToken: 0 })
    await waitFor(() => expect(exactCalls('/boards/880001')).toBe(1))
    rerender(
      <MemoryRouter>
        <BoardBody code="880001" refreshToken={1} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByText('+2.50%')).toBeTruthy())

    rejectStale(new Error('过期板块请求失败'))
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })

    expect(screen.getByText('+2.50%')).toBeTruthy()
    expect(screen.queryByText(/过期板块请求失败/)).toBeNull()
  })
})
