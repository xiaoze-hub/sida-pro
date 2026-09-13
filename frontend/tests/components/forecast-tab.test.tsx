// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { ToastProvider } from '@panwatch/base-ui/components/ui/toast'

/**
 * Task 16 标签「预测」守六件事:
 *
 * ① **惰性: 没挂载就不加载** —— `ForecastTab.tsx` 用的是 `lazy(importForecastPage)`(工厂
 *    `() => import('@/pages/Forecast')`)。断言: 未渲染本标签时工厂 **0 次**; 渲染后恰好
 *    **1 次** —— 这就是计划 Step 1「仅激活标签时加载」的机制本身(Task 17 的 `TabPanel` 只渲染
 *    激活标签 ⇒ 非预测标签下工厂一次都不会跑、预测 chunk 不下载)。工厂计数由本文件对 `react` 的
 *    `lazy` 做**薄包装**取得(仍委托真实 `lazy`), 不是断言某个 mock 的调用 —— 见下「工厂计数怎么来」。
 * ② **pending 出 fallback, 预测页一棵都不上屏** —— chunk 在途(永不 resolve)⇒ `Suspense`
 *    fallback(「加载预测页(四模型)…」)在屏, 预测页**零渲染**, 且屏上无任何编造的预测结果/数值。
 * ③ **渲染抛错 ⇒ 就近兜底**(成因域文案 + 真实错误消息 + 「重试」, 且**不**冒到外层整页兜底)。
 *    这条覆盖「页内渲染报错」这一**成因域分支**(与 ④ 的"下载失败"共同构成兜底文案的两个成因)。
 * ④ **chunk 下载失败 = `import()` **真** reject ⇒ 就近兜底, 且「重试」真重发 `import()`** ——
 *    与 ③ 不同, 这条让**动态 `import()` 本身失败**(受控替身模块在 `import()` 时返回 rejected
 *    promise), 即真实的 module-load 失败路径。断言: 屏上 `forecast-tab-error`(成因域 + 真实错误
 *    消息 + 「重试」), **不**冒到外层整页兜底(本用例另挂 `OuterBoundary` 当哨兵); 此时
 *    **工厂计数 == 1**(确实跑过、且只跑一次)。点「重试」⇒ 工厂**再次**被调用(**计数 == 2**),
 *    第二次成功 ⇒ 预测页上屏。计数 == 2 是"重试真重挂载 + 真重发"的**直接**证据(不是只清错误位)。
 *    ⚠ 为什么"重试能真重发"需要实现配合: `React.lazy` 会把 reject **永久缓存**(payload `_status`
 *    置 `Rejected` 后每次都 `throw` 同一个错、**不再调用工厂**)。若 `lazy()` 实例是**模块级常量**,
 *    重试只会重挂载、工厂停在 1、错误永远复现(本仓库实测取证)。`ForecastTab.tsx` 因此把 `lazy()`
 *    实例放组件 state, 「重试」时换**全新**实例 ⇒ 工厂被再次调用。本文件 ④ 的"计数 == 2"正是这条
 *    的回归守卫; 若有人把实现改回模块级常量, ④ 会立刻失败(计数停在 1)。
 * ⑤ **形态差异可观测(未走 `InsightProvider`)** —— 与 T11–T15 不同, 本标签不引用
 *    `InsightProvider`(控制器裁定: 预测页自包含)。断言挂载期间真 `@panwatch/api` 零调用 ——
 *    若有人给本标签补上 provider 取数, 这条会立刻失败。
 * ⑥ **`ForecastPage` 的 props 面(接口事实, 由 TS 守卫)** —— 它**只**吃可选 `initialSymbol`
 *    (v0.6.0 遗留②: 本标签据此预选标的)。对真实模块用 `@ts-expect-error` 断言
 *    `<ForecastPage symbol="600519" />` 类型不通过(错属性名): 若将来 `Forecast.tsx` 把
 *    `symbol` 也收进来, 该指令会变成"未使用" ⇒ 类型检查报错, 逼后人重审本标签的透传契约。
 *    ⚠ **诚实说明谁在检查这条**: 本项目 `frontend/tsconfig.json` 的 `include` 是
 *    `["src", "packages/[star]/src"]`(此处 `[star]` 代指星号, 避免注释被提前闭合), `tests/`
 *    **不在**其中, 且 `eslint.config.js` 的 `files` 也不含 `tests/` ⇒ 现有门禁 `npx tsc -b` /
 *    `npx eslint .` **都不会**对这个测试文件做类型检查(已实测: 往本文件塞一个必然类型错误,
 *    `tsc -b`/`eslint` 仍全绿)。
 *    故本用例的运行时断言(`expect(bad).toBeTruthy()`)只证明"这行能被求值", **不**证明类型不通过;
 *    它的**全部价值**在编译期。已实测的**手工**验证命令(把 tests 单独纳入一次类型检查):
 *      `npx tsc --noEmit` + 一个 `tsconfig` 覆盖 `include: ["tests/components/forecast-tab.test.tsx"]`
 *      ⇒ 保留本指令时 0 error; **删掉本指令立刻报 TS2322**(`Property 'symbol' does not exist`)
 *      —— 证明该指令确实在抑制一个**真实**类型错误、不是摆设。
 *    **不要**把这条当成"现有门禁已守住": 除非将来把 `tests/` 纳入某个 tsc 项目, 否则改
 *    `Forecast.tsx` 加 props 不会让 `tsc -b` 失败。见 task-16-report 对这条的登记。
 *
 * 怎么做到"可控地模拟 chunk 加载/失败"(**不替掉边界**: 边界本身要被真实测到):
 *  - `vi.mock('@/pages/Forecast')` 把**既有生产页**(本任务不改它)换成受控替身, 但替身的
 *    `default` 必须是一个**普通组件**(**不能**再是 `React.lazy` 对象)。原因(已实测取证):
 *    本标签自身那层 `lazy()` 会把模块的 `default` 当成组件类型; 若 `default` 本身是个 lazy
 *    对象, 外层 lazy 解析出来的"组件"仍是个 lazy 对象 ⇒ React 报
 *    "Element type is invalid. Received a promise that resolves to: [object Object].
 *     Lazy element type must resolve to a class or function. Did you wrap a component in
 *     React.lazy() more than once?"(即**双层 lazy**)。
 *  - 于是: **只有一层 lazy**(本标签的那层), chunk 的 pending / reject 由本文件对 `react.lazy`
 *    的薄包装读 `mocks.gate` 决定 —— `pending` 返回**永不 settle** 的 promise(真实"在途"),
 *    `reject` 返回 **rejected** promise(真实"下载失败")。这两条路径正是本标签要覆盖的边界行为,
 *    且**是 `import()` 层面的失败**, 不是"已 resolve 的组件再抛错"。
 *  - `@panwatch/api` 也替成真模块形状的最小面(getToken/clearToken/fetchAPI/stocksApi.list),
 *    即使将来预测页被真的加载(如断言⑥)也不会打到网络。
 *
 * 工厂计数怎么来(与"真数据纪律"一致, 见下):
 *  - 断言①/④ 要"`import()` 工厂被调用几次"。这个计数**必须**来自真实 `React.lazy` 调用工厂的时刻,
 *    故本文件对 `react` 做 `vi.mock` —— 但**只**包一层 `lazy`(记一次 + 按 `gate` 决定返回什么),
 *    其余 `react` 导出**原样透传**(`await importOriginal()` 展开)。这样 `@testing-library/react` /
 *    `Suspense` / `useState` / `AppErrorBoundary` 用的都还是真实 react(已实测: 与 `MemoryRouter` +
 *    `ToastProvider` 共存无碍), 计数的也确实是留给生产代码里那层 `lazy` 的工厂。
 *
 * 真数据纪律: 被替换的是**模块边界**(既有生产页 + 网络层 + `React.lazy` 的这一层包装), 本标签
 * 自身的代码(懒边界/兜底/文案/token 用法)与 `AppErrorBoundary` 全走真实实现。
 */

const mocks = vi.hoisted(() => ({
  /** 真实 `React.lazy` **调用工厂**的次数(每次托付给 `lazy` 的工厂被执行时 +1)。 */
  load: vi.fn(),
  /** 下一次 chunk 落地的结果(用例按需切换; 每次工厂调用只读一次)。 */
  gate: { mode: 'ok' as 'pending' | 'ok' | 'reject', message: '' },
  /** 替身组件被渲染的次数(用于证明 pending 时"零渲染")。 */
  render: vi.fn(),
  fetchAPI: vi.fn(),
  stocksList: vi.fn(),
}))

vi.mock('react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react')>()
  return {
    ...actual,
    // 只包 `lazy`: 计数 + 按 gate 决定"这次 chunk 落地/在途/失败"。其余导出原样透传。
    // 签名与 `@types/react` 的 `lazy<T extends ComponentType<any>>(load)` 保持一致(否则类型不兼容)。
    lazy: <T extends import('react').ComponentType<unknown>>(
      factory: () => Promise<{ default: T }>,
    ): import('react').LazyExoticComponent<T> =>
      actual.lazy<T>(() => {
        mocks.load()
        if (mocks.gate.mode === 'pending') return new Promise<{ default: T }>(() => {})
        if (mocks.gate.mode === 'reject') return Promise.reject(new Error(mocks.gate.message))
        return factory()
      }),
  }
})

/** 预测页替身正文: 只给一个可断言的标记文本; `initialSymbol` 回显以便断言预选透传。 */
function FakeForecastPage({ initialSymbol }: { initialSymbol?: string }) {
  mocks.render()
  return (
    <div data-testid="fake-forecast-page" data-initial-symbol={initialSymbol ?? ''}>
      FORECAST-PAGE-RENDERED
    </div>
  )
}

vi.mock('@/pages/Forecast', () => ({
  // 注意: `default` 必须是**普通组件**(详见文件头注「双层 lazy」)。chunk 的 pending / reject
  // 由上面 react.lazy 的包装在 `import()` 时决定 —— 这里不再需要"从组件体里抛错"的权宜写法。
  default: FakeForecastPage,
}))

vi.mock('@panwatch/api', () => ({
  getToken: () => null,
  clearToken: () => {},
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
  stocksApi: { list: (...a: unknown[]) => mocks.stocksList(...a) },
}))

import ForecastTab from '@/pages/workbench/tabs/ForecastTab'
// 本文件把 `@/pages/Forecast` 换成了受控替身 ⇒ 这里的 `ForecastPage` 就是替身;
// 断言⑥ 的"props 面只有 initialSymbol"由 `@ts-expect-error` 守住 —— 它读的是**类型**, 而 `vi.mock` 不改类型。
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
  mocks.gate.message = ''
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
  it('未挂载 ⇒ 惰性工厂零调用; 挂载后落地上屏, 且 fallback 退场', async () => {
    // 未渲染本标签 ⇒ 工厂一次都没被调用("仅激活标签时加载"的机制本身)
    expect(mocks.load).not.toHaveBeenCalled()

    render(tree())

    expect(await screen.findByTestId('fake-forecast-page')).toBeTruthy()
    // 恰一次: chunk 落地后 lazy 不再重复调用工厂(重渲染不重发)
    expect(mocks.load).toHaveBeenCalledTimes(1)
    // fallback 退场, 无残影
    expect(screen.queryByText('加载预测页(四模型)…')).toBeNull()
    // 预选透传: 工作台 symbol 作 `initialSymbol` 进了预测页(v0.6.0 遗留②)
    expect(screen.getByTestId('fake-forecast-page').getAttribute('data-initial-symbol')).toBe('002636')
    // 口径行如实: 已按工作台标的预填(不再声称"不预填")
    expect(screen.getByTestId('forecast-tab').textContent).toContain('已按工作台标的预填代码')
  })

  it('pending ⇒ 出 fallback, 预测页零渲染(不预填/不假装已加载)', async () => {
    mocks.gate.mode = 'pending'
    render(tree())

    expect(await screen.findByText('加载预测页(四模型)…')).toBeTruthy()
    // chunk 在途: 工厂确实跑了(1 次), 但替身组件一次都没渲染, 也没有错误框
    expect(mocks.load).toHaveBeenCalledTimes(1)
    expect(mocks.render).not.toHaveBeenCalled()
    expect(screen.queryByTestId('fake-forecast-page')).toBeNull()
    expect(screen.queryByTestId('forecast-tab-error')).toBeNull()
    // 不编造任何预测数值/结果
    expect(screen.getByTestId('forecast-tab').textContent).not.toContain('%')
  })
})

describe('Task 16 预测: chunk 失败就近兜底 + 重试真重发 import()', () => {
  it('渲染抛错 ⇒ 标签内兜底(不冒到整页), 「重试」重挂载后再上屏', async () => {
    // 成因域的另一半("页内渲染报错"): 模块正常落地, 但替身组件渲染时抛错。
    // 用 gate=ok 让 lazy 正常 resolve, 再让替身组件抛 —— 这是"已 resolve 组件渲染失败"的真实路径。
    const Boom = () => {
      throw new Error('render blew up inside ForecastPage')
    }
    vi.spyOn(await import('@/pages/Forecast'), 'default').mockImplementation(Boom)
    render(tree())

    // 就近兜底: 成因域文案 + 真实错误消息(不猜"引擎未启动")
    const box = await screen.findByTestId('forecast-tab-error')
    expect(box.textContent).toContain('预测页加载失败')
    expect(box.textContent).toContain('预测页代码块下载失败 / 页内渲染报错')
    expect(box.textContent).toContain('render blew up inside ForecastPage')
    // 屏上文案不许带 markdown 星号(用户可见文本; 曾出现 `**不作**` 直出)
    expect(box.textContent).not.toContain('**')
    // 错误没有冒出本标签(外层整页兜底哨兵未触发)
    expect(outerProbe).toBe(0)
    expect(screen.queryByText('页面遇到了问题')).toBeNull()
    // 失败态下不假装已恢复
    expect(screen.queryByTestId('fake-forecast-page')).toBeNull()

    // 换回正常替身, 「重试」= boundary reset + 重挂 Suspense ⇒ 预测页重新挂载并成功
    vi.mocked(ForecastPage).mockRestore()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('fake-forecast-page')).toBeTruthy()
    expect(screen.queryByTestId('forecast-tab-error')).toBeNull()
  })

  it('chunk 下载失败(import() 真 reject) ⇒ 标签内兜底 + 工厂计数 1; 「重试」⇒ 计数 2 且上屏', async () => {
    // 真实的 module-load 失败: 工厂返回 rejected promise(等价 chunk 被换掉 / 断网)
    mocks.gate.mode = 'reject'
    mocks.gate.message = 'Failed to fetch dynamically imported module'
    render(tree())

    const box = await screen.findByTestId('forecast-tab-error')
    expect(box.textContent).toContain('预测页代码块下载失败 / 页内渲染报错')
    // 真实错误消息如实透出(不是编造的"引擎未启动")
    expect(box.textContent).toContain('Failed to fetch dynamically imported module')
    // 工厂确实跑过、且只跑一次
    expect(mocks.load).toHaveBeenCalledTimes(1)
    // 失败态: 替身组件零渲染、不冒到外层整页兜底
    expect(screen.queryByTestId('fake-forecast-page')).toBeNull()
    expect(mocks.render).not.toHaveBeenCalled()
    expect(outerProbe).toBe(0)

    // 「重试」= boundary reset + 换**全新** lazy 实例 + 自增 key 重挂载 ⇒ import() 真重发
    mocks.gate.mode = 'ok'
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('fake-forecast-page')).toBeTruthy()
    // 关键断言: 工厂被**再次**调用(计数 2)⇒ 重试不是"只清错误位", 而是真重发 import()
    expect(mocks.load).toHaveBeenCalledTimes(2)
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

describe('Task 16 预测: ForecastPage props 面(接口事实由 TS 守卫)', () => {
  it('真实模块只吃 initialSymbol ⇒ 强塞 symbol 类型不通过', () => {
    // 预选走 `initialSymbol`(v0.6.0 遗留②); `symbol` 属错属性名, 必须类型报错。
    // 若将来 Forecast.tsx 把 `symbol` 也收进来, 这行会变成"未使用的 @ts-expect-error"
    // ⇒ 类型检查失败 ⇒ 逼后人重审本标签的透传契约。
    // @ts-expect-error ForecastPage 只接受 initialSymbol, 没有 symbol
    const bad = <ForecastPage symbol="600519" />
    // ↓ 运行时断言**不含**类型价值(元素对象恒 truthy): 本条的全部价值在上一行的 `@ts-expect-error`
    //   (编译期)。注意 tests/ 不在 tsconfig.json 的 include 里, 现有 `tsc -b`/`eslint .` **不**检查
    //   本文件 —— 详见文件头注断言⑥ 的诚实说明与 task-16-report §4。
    expect(bad).toBeTruthy()
  })
})
