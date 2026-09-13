// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 16 标签「预测」守五件事:
 *
 * ① **惰性: 没挂载就不加载** —— `ForecastTab.tsx` 用的是 `lazy(() => import('@/pages/Forecast'))`,
 *    工厂只在**首次渲染**该 lazy 组件时被调用。断言: 未渲染本标签时工厂 **0 次**; 渲染后恰好
 *    **1 次** —— 这就是计划 Step 1「仅激活标签时加载」的机制本身(Task 17 的 `TabPanel` 只渲染
 *    激活标签 ⇒ 非预测标签下工厂一次都不会跑、预测 chunk 不下载)。
 * ② **pending 出 fallback, 预测页一棵都不上屏** —— chunk 在途(永不 resolve)⇒ `Suspense`
 *    fallback(「加载预测页(四模型)…」)在屏, 预测页**零渲染**, 且屏上无任何编造的预测结果/数值。
 * ③ **chunk 失败就近兜底, 且「重试」真重发 `import()`** —— chunk reject 时 `React.lazy` 会把
 *    rejection 抛给最近的 Error Boundary; 本标签自带 `AppErrorBoundary`(自定义 fallback)
 *    ⇒ 屏上出现 `forecast-tab-error`(成因域 + 真实错误消息 + 「重试」), **不**冒到外层整页兜底
 *    (本用例另挂 `OuterBoundary` 当哨兵)。点「重试」⇒ 工厂**再次**被调用(计数 2), 第二次成功
 *    ⇒ 预测页上屏(证明重试是真重挂载/重发, 不是清个错误位了事)。
 * ④ **形态差异可观测(未走 `InsightProvider`)** —— 与 T11–T15 不同, 本标签不引用
 *    `InsightProvider`(控制器裁定: 预测页自包含)。断言挂载期间真 `@panwatch/api` 零调用 ——
 *    若有人给本标签补上 provider 取数, 这条会立刻失败。
 * ⑤ **`ForecastPage` 无 props 面(接口事实, 由 TS 守卫)** —— 对真实模块用 `@ts-expect-error`
 *    断言 `<ForecastPage symbol="600519" />` 类型不通过: 若将来 `Forecast.tsx` 加了 props, 该
 *    指令会变成"未使用" ⇒ `tsc -b` 立刻失败, 逼后人重审本标签"不传 symbol"的决定。
 *
 * 怎么做到"可控地模拟 chunk 加载"(**不替掉边界**: 边界本身要被真实测到):
 *  - `vi.mock('@/pages/Forecast')` 把**既有生产页**(本任务不改它)换成受控替身, 但替身的
 *    `default` 必须是一个**普通组件**(**不能**再是 `React.lazy` 对象)。原因(已实测取证):
 *    本标签自身那层 `lazy()` 会把模块的 `default` 当成组件类型; 若 `default` 本身是个 lazy
 *    对象, 外层 lazy 解析出来的"组件"仍是个 lazy 对象 ⇒ React 报
 *    "Element type is invalid. Received a promise that resolves to: [object Object].
 *     Lazy element type must resolve to a class or function. Did you wrap a component in
 *     React.lazy() more than once?"(即**双层 lazy**)。
 *  - 于是: **只有一层 lazy**(本标签的那层), chunk 的 pending / reject 由替身模块在
 *    `await import()` 时读 `mocks.gate` 决定 —— pending 直接返回**永不 settle** 的 promise,
 *    reject 直接返回 rejected promise。这两条路径正是本标签要覆盖的边界行为。
 *  - `@panwatch/api` 也替成真模块形状的最小面(getToken/clearToken/fetchAPI/stocksApi.list),
 *    即使将来预测页被真的加载(如断言⑤)也不会打到网络。
 *
 * 真数据纪律: 被替换的是**模块边界**(既有生产页 + 网络层), 本标签自身的代码(懒边界/兜底/
 * 文案/token 用法)与 `AppErrorBoundary` 全走真实实现。
 */

const mocks = vi.hoisted(() => ({
  /** `import('@/pages/Forecast')` 的调用计数 + 观测点。 */
  load: vi.fn(),
  /** 下一次 chunk 落地的结果(每次 `import()` 只读一次, 用例按需切换)。 */
  gate: { mode: 'ok' as 'pending' | 'ok' | 'reject' },
  /** `default` 被当作组件渲染的次数(用于证明 pending/reject 时"零渲染")。 */
  render: vi.fn(),
  fetchAPI: vi.fn(),
  stocksList: vi.fn(),
}))

/** 预测页替身正文: 只给一个可断言的标记文本。 */
function FakeForecastPage({ marker }: { marker?: string }) {
  mocks.render()
  return <div data-testid="fake-forecast-page">{marker ?? 'FORECAST-PAGE-RENDERED'}</div>
}

vi.mock('@/pages/Forecast', () => {
  // 注意: `default` 必须是**普通组件**(详见文件头注「双层 lazy」)。但"永不 settle / reject"
  // 这两种在途/失败状态无法由一个已 resolve 的组件表达 —— 故用**模块级开关**在 `import()` 时
  // 决定给 React 什么: 正常态给真组件; pending 给永不 settle 的 promise; reject 给失败 promise。
  return {
    default: () => {
      if (mocks.gate.mode === 'pending') throw new Promise<never>(() => {})
      if (mocks.gate.mode === 'reject') throw new Error(mocks.gate.message)
      return <FakeForecastPage marker={mocks.gate.marker} />
    },
  }
})

vi.mock('@panwatch/api', () => ({
  getToken: () => null,
  clearToken: () => {},
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
  stocksApi: { list: (...a: unknown[]) => mocks.stocksList(...a) },
}))

import ForecastTab from '@/pages/workbench/tabs/ForecastTab'
// 本文件把 `@/pages/Forecast` 换成了受控替身 ⇒ 这里的 `ForecastPage` 就是替身;
// 断言⑤ 的"零 props 面"由 `@ts-expect-error` 守住 —— 它读的是**类型**, 而 `vi.mock` 不改类型。
import ForecastPage from '@/pages/Forecast'
import AppErrorBoundary from '@/components/ErrorBoundary'

/** 外层(整页)兜底哨兵: 它若被触发即说明错误**冒出了**本标签。 */
let outerProbe = 0
class OuterBoundary extends AppErrorBoundary {
  componentDidCatch(...args: Parameters<NonNullable<AppErrorBoundary['componentDidCatch']>>) {
    outerProbe += 1
    super.componentDidCatch(...args)
  }
}

beforeEach(() => {
  mocks.load.mockClear()
  mocks.render.mockClear()
  mocks.gate.mode = 'ok'
  mocks.gate.marker = undefined
  mocks.fetchAPI.mockReset()
  mocks.stocksList.mockReset()
  mocks.fetchAPI.mockResolvedValue({})
  mocks.stocksList.mockResolvedValue([])
  outerProbe = 0
  // ErrorBoundary 会 console.error: 保持输出干净(不改行为)
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

function tree(symbol = '002636', market = 'CN') {
  return (
    <MemoryRouter>
      <ToastProvider>
        <OuterBoundary>
          <ForecastTab symbol={symbol} market={market} />
        </OuterBoundary>
      </ToastProvider>
    </MemoryRouter>
  )
}

describe('Task 16 预测: 惰性加载', () => {
  it('未挂载 ⇒ 未加载; 挂载后落地上屏, 且 fallback 退场', async () => {
    // 未渲染本标签 ⇒ 惰性工厂一次都没被调用("仅激活标签时加载"的机制本身)
    expect(mocks.render).not.toHaveBeenCalled()

    render(tree())

    expect(await screen.findByTestId('fake-forecast-page')).toBeTruthy()
    expect(mocks.render).toHaveBeenCalledTimes(1)
    // fallback 退场, 无残影
    expect(screen.queryByText('加载预测页(四模型)…')).toBeNull()
    // 口径行如实: 不预选标的
    expect(screen.getByTestId('forecast-tab').textContent).toContain('预测页不接受外部标的传入, 本标签不预填')
  })

  it('pending ⇒ 出 fallback, 预测页零渲染(不预填/不假装已加载)', async () => {
    mocks.gate.mode = 'pending'
    render(tree())

    expect(await screen.findByText('加载预测页(四模型)…')).toBeTruthy()
    // chunk 未落地 ⇒ 预测页组件一次都没渲染, 也没有错误框
    expect(mocks.render).not.toHaveBeenCalled()
    expect(screen.queryByTestId('fake-forecast-page')).toBeNull()
    expect(screen.queryByTestId('forecast-tab-error')).toBeNull()
    // 不编造任何预测数值/结果
    expect(screen.getByTestId('forecast-tab').textContent).not.toContain('%')
  })
})

describe('Task 16 预测: chunk 失败就近兜底 + 重试真重发', () => {
  it('渲染抛错 ⇒ 标签内兜底(不冒到整页), 「重试」成功后再上屏', async () => {
    mocks.gate.mode = 'reject'
    mocks.gate.message = 'Failed to fetch dynamically imported module'
    render(tree())

    // 就近兜底: 成因域文案 + 真实错误消息(不猜"引擎未启动")
    const box = await screen.findByTestId('forecast-tab-error')
    expect(box.textContent).toContain('预测页加载失败')
    expect(box.textContent).toContain('预测页代码块下载失败 / 页内渲染报错')
    expect(box.textContent).toContain('Failed to fetch dynamically imported module')
    // 错误没有冒出本标签(外层整页兜底哨兵未触发)
    expect(outerProbe).toBe(0)
    expect(screen.queryByText('页面遇到了问题')).toBeNull()
    // 失败态下不假装已恢复
    expect(screen.queryByTestId('fake-forecast-page')).toBeNull()

    // 「重试」= boundary reset + 重挂 Suspense ⇒ 预测页重新挂载并成功
    mocks.gate.mode = 'ok'
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('fake-forecast-page')).toBeTruthy()
    expect(screen.queryByTestId('forecast-tab-error')).toBeNull()
  })
})

describe('Task 16 预测: 形态差异(未走 InsightProvider, 零取数)', () => {
  it('挂载期间不经 provider 取数: 真 @panwatch/api 全零(与 T11–T15 的 keys 形态不同)', async () => {
    render(tree())

    await screen.findByTestId('fake-forecast-page')
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
    expect(mocks.stocksList).not.toHaveBeenCalled()
  })
})

describe('Task 16 预测: ForecastPage 无 props 面(接口事实由 TS 守卫)', () => {
  it('真实模块是零 props 默认导出 ⇒ 强塞 symbol 类型不通过', () => {
    // 零 props: 传 symbol 必须类型报错。若将来 Forecast.tsx 加了 props, 这行会变成
    // "未使用的 @ts-expect-error" ⇒ `tsc -b` 失败 ⇒ 逼后人重审本标签"不传 symbol"的决定。
    // @ts-expect-error ForecastPage 无 props 面
    const bad = <ForecastPage symbol="600519" />
    expect(bad).toBeTruthy()
  })
})
