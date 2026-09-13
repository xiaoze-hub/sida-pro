import { lazy, Suspense, useState } from 'react'
import { Loader2, AlertTriangle } from 'lucide-react'
import AppErrorBoundary from '@/components/ErrorBoundary'

/**
 * 工作台标签「预测」(工作台 v2 三合一, Task 16)。
 *
 * 落位(spec §4.3 / 计划 Task 16): **惰性内嵌**既有预测页 `frontend/src/pages/Forecast.tsx`
 * —— 四模型(`/forecast/predict` 预测 + `/forecast/backtest` 回测 + `/forecast/history` 记录
 * + `/forecast/weights` 权重 + `/forecast/report/*` 报告), 另含该页自带的引擎健康
 * (`/forecast/health`)与自选/搜索入口。本标签**不重写**预测页里的任何一段(输入区/结果区/
 * 模型对比/历史表/报告都在 `Forecast.tsx` 内), 自建的只有: 一个 `lazy` 边界 + 一层
 * `AppErrorBoundary` + 加载/错误两条如实文案(见下)。
 *
 * 惰性怎么工作(计划 Step 1 的字面要求: `React.lazy` + `Suspense`, 仅激活标签时加载):
 *  - `lazy(importForecastPage)` 的工厂(即 `() => import('@/pages/Forecast')`)只在**首次渲染**
 *    这个 lazy 组件时调用 ⇒ Vite/Rollup 把 `Forecast.tsx` 及其依赖(`ReactMarkdown` /
 *    `ForecastConeChart` / `@panwatch/api` 的 stocksApi 等)拆成**独立 chunk**, 与工作台首屏包分离;
 *  - 本文件是**默认导出**, 但索引本身是静态导入(与 T11–T15 同形态: 标签组件都不在路由表上)
 *    ⇒ 只要没人渲染本组件就不会触发工厂。Task 17 的 `TabPanel` 按 `?tab=` **只渲染激活标签**
 *    ⇒ 非预测标签下这段 `import()` 一次都不发生(首屏不拉预测接口、不下载预测 chunk);
 *    `?tab=forecast` 时才 `import()`、先出 `Suspense` fallback、chunk 落地后换成预测页。
 *  - `lazy()` 实例用 `useState` **持有在组件内**(惰性初始化, 只建一次; 而非模块级常量):
 *    同一 attempt 内 state 不变(不白闪), 「重试」时 `setForecastPage` 换全新实例 —— 这是
 *    "重试能真重发 `import()`"的前提, 理由见下第 2 条。
 *  - 与 `Quote.tsx` 的既有 `lazy(() => import('@/pages/Forecast'))` **同一模块、同一说明符** ⇒
 *    打包器把它们归到**同一个 chunk**(两处消费方共享, 不产生重复副本); P3 退役 `Quote.tsx`
 *    时本标签是该 chunk 的剩余消费方, 行为不变(说明符未改)。
 *
 * `ForecastPage` 要不要参数? **不要** —— 已核实其导出面:
 *  - 签名是 `export default function ForecastPage()`(**零 props**), 内部 `symbol` 是
 *    `useState('')` 的本地状态, 由用户在该页自带的搜索框/自选下拉里选;
 *  - **不读路由** —— 该文件里没有 `useParams`/`useSearchParams`/`useLocation`/`useNavigate`
 *    (grep 零命中), 也不读 `?symbol=`; 四个子功能走的是**页内页签状态**而非
 *    `/forecast/predict|backtest|...` 路由(brief 里那串路径实为该页调用的**端点**)⇒ 嵌进
 *    工作台不需要额外包一层 Router, `Quote.tsx` 里也是 `<ForecastPage />` 裸渲染。
 *  因此本标签**不**把工作台的 `symbol` 透传进去(透传会被 TS 直接拒掉: 该组件无 props 面),
 *  也**不**伪造"已预选标的"的观感 —— 见下「诚实空态」第 3 条。
 *  - `market` 同理不需要: 预测页只按 6 位 A 股代码取数(`/^\d{6}$/` 校验), 与市场无关。
 *
 * 与兄弟标签的**形态差异(有意, 非疏漏)**: T11–T15 都是"标签自带 `InsightProvider` + 按 keys
 * 取数 + `data-testid="*-tab"` 根节点"; 本标签**不引用 `InsightProvider`**(控制器裁定, 见
 * progress「Task 16 Ruling」: 预测页自包含, 不走 provider 的 `ResourceKey` 资源面), 且预测页
 * 是既有生产页、**一字未改** ⇒ 不给它套额外的 `data-testid` 包裹层(那要动 `Forecast.tsx`)。
 * 本条口径的机器守卫见测试里的「形态差异」用例(断言挂载期间真 `@panwatch/api` 零调用
 * —— 正是"没走 provider"的可观测后果)。
 *
 * 诚实加载 / 错误态(never fabricate):
 *  1. **加载中** —— `Suspense` fallback 是一行如实文案(带 true 的 spinner), **不**编造骨架屏
 *     内容、**不**预填任何预测数值;
 *  2. **chunk 加载失败** —— `lazy` 的 `import()` reject(离线 / 旧部署下 chunk 被换掉 / 网络
 *     断)时, React 会把该 promise 的 rejection **抛给最近的 Error Boundary**。若本标签不设
 *     Boundary, Task 17 之后这个错会冒到工作台**外面**的 `AppErrorBoundary` 全屏兜底 ——
 *     一个标签炸掉会导致"预测/回测/历史/权重/报告"五处都进不去(它们**全在同一个 chunk** 里)。
 *     故本标签就近包一层 `AppErrorBoundary`(复用既有 `@/components/ErrorBoundary`, 不新建),
 *     用**自定义 fallback** 把范围收敛成标签内一块: 文案如实说明**原因域**(代码块下载失败 /
 *     页内渲染报错)并给出可点的「重试」; 重试 = boundary 自身 `reset` + `setForecastPage(新 lazy 实例)`
 *     + 自增 `attempt` 换 `Suspense` 的 `key`(重挂载)⇒ `import()` 真的**再次执行**
 *     (成功的 `import()` 会被模块缓存命中, 失败的则重发)→ 无需整页刷新。
 *     **为什么必须换新实例**(实测取证, 见组件内注释与 task-16-report): 只重挂载而不换 `lazy()`
 *     实例时, `React.lazy` 会把 reject **永久缓存**, 工厂**不再被调用**(实测工厂计数停在 1、
 *     错误一直复现、页面永远不上屏)。故本标签**不能**用模块级 `lazy()` 常量。
 *     **不假装**已恢复、**不吞掉**错误(`console.error` 由 boundary 打)。
 *  3. **未预选标的** —— 预测页以空标的挂载, 用户要预测哪只票就在该页搜索框里选(默认不预填)。
 *     这是**产品事实**(上面已核实其无 props/无路由入参), 本标签如实写在口径行里, **不**声称
 *     "已带入 002636", 也不额外造一个"帮用户填好"的假象。若将来要预选, 需给 `Forecast.tsx`
 *     加 props 或读查询参数 —— 属改既有生产页, 超出本任务授权(见 task-16-report concern 1)。
 *
 * 去重(§三 去重表): 现价/名称/涨跌归带1 `HeaderBand`; 本标签内只有预测页自己的输入区(搜索框
 * 里出现代码文本、已选行)会显示标的代码 —— 那是**用户在该页输入回显**, 与带1 的行情快照
 * 不是同一种数据(带1 给的是实时行情), 本文件不重复渲染任何行情数值。
 *
 * 归属 `src/pages/workbench/tabs/`: 与 T11–T15 同层, 由 Task 17 的 `TabPanel` 按 `?tab=forecast`
 * 挂载; 本文件不新增 biz-ui 依赖方向(只消费 app 层 `@/components/ErrorBoundary` 与 base-ui 图标)。
 */
/**
 * lazy 边界的**模块级工厂**(纯函数: 每次调用返回 `import()` 的 thenable)。
 *
 * 为什么要把它和 `lazy()` 实例拆开(而非直接 `const ForecastPage = lazy(...)`):
 *  `React.lazy` 会把失败**永久缓存**在 payload 上(react.development.js `lazyInitializer`:
 *  reject 时 `_status = Rejected`. 此后每次渲染都 `throw payload._result` 同一个错, **不再调用工厂**)。
 *  若用模块级 `lazy()` 常量, 则「重试」只能重挂载, 工厂不会再跑 ⇒ 一次 chunk 下载失败后**永远**恢复
 *  不了(除非整页刷新)。故本标签把 `lazy()` 实例放组件 state(见组件内 `useState`), 「重试」时换**全新**
 *  实例, 让「重试」真的重新执行 `import()` —— 这才是文件头注第 2 条承诺的行为, 也有测试断言工厂
 *  计数 == 2 兜住。
 */
const importForecastPage = () => import('@/pages/Forecast')

/** 加载态文案(与 `Quote.tsx`「加载预测…」措辞一致, 避免同一 chunk 两处说法不同)。 */
const LOADING_TEXT = '加载预测页(四模型)…'

/**
 * chunk 下载失败 / 预测页渲染抛错时的**就近**兜底。文案只陈述**成因域**(代码块 vs 渲染),
 * 不猜具体原因、不假称已恢复; 「重试」真重挂 `Suspense`(见文件头注第 2 条)。
 */
function ForecastFallback({ error, onRetry }: { error: Error; onRetry: () => void }) {
  return (
    <div
      data-testid="forecast-tab-error"
      role="alert"
      className="rounded border border-border/60 bg-muted/20 p-3 text-[12px]"
    >
      <div className="flex items-center gap-2 text-foreground">
        <AlertTriangle className="h-3.5 w-3.5 text-amber-600" />
        <span className="font-medium">预测页加载失败</span>
      </div>
      <div className="mt-1 text-muted-foreground">
        成因域: 预测页代码块下载失败 / 页内渲染报错(此处不作"引擎未启动"的推断 —— 该页有自己的引擎状态位)
      </div>
      <div className="mt-1 font-mono text-[11px] text-muted-foreground break-all">{error.message || String(error)}</div>
      <button
        type="button"
        onClick={onRetry}
        className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-[12px] hover:bg-accent transition-colors"
      >
        重试
      </button>
    </div>
  )
}

/**
 * 标签入口。`symbol` / `market` **有意不消费**(理由见文件头注「ForecastPage 要不要参数?」):
 * 预测页无 props 面、不读路由 ⇒ 传进去无落点。签名仍与兄弟标签同形(`{ symbol, market }`),
 * 好让 Task 17 的 `TabPanel` 用同一套调用约定接线; 两个属性在签名里保留类型信息, 不做任何
 * 静默改写(不用它们拼请求、不用它们预选标的)。
 */
export default function ForecastTab({ symbol, market }: { symbol: string; market: string }) {
  // 两个属性**确实不消费**(见上): 声明式 `void` 保留签名文档价值 + 给读代码的人一个显式"已知未用"
  // 信号(参数表里保留它们, 是为了与 T11–T15 的 `{ symbol, market }` 调用约定同形, 便于 Task 17 统一接线)。
  void symbol
  void market

  /**
   * `lazy()` 实例的**持有方式**: 放 state(惰性初始化, 只建一次)而不是模块级常量。
   *
   * 为什么不能是模块级常量(实测取证): `React.lazy` 会把 reject **永久缓存**在 payload 上
   * (react.development.js `lazyInitializer` 里 reject 分支把 `_status` 置 `Rejected`; 此后每次
   * 渲染都 `throw payload._result` 同一个错, **不再调用工厂**)。若实例是模块级常量, 「重试」只能
   * 重挂载, 工厂不会重跑 ⇒ 一次 chunk 下载失败后**永远**恢复不了(除非整页刷新)。
   * 放 state 则能在「重试」时用 `setForecastPage` 换一个**全新实例**(payload = `Uninitialized`)⇒
   * 工厂被再次调用、`import()` 真重发。同一 attempt 内 state 不变 ⇒ 不会每帧新建、不白闪。
   */
  const [ForecastPage, setForecastPage] = useState(() => lazy(importForecastPage))

  /**
   * 重试计数: 作 `Suspense` 子树的 `key`(自增即整块重挂载)。换 `lazy()` 实例由 `onRetry` 里
   * 的 `setForecastPage` 负责 —— 两者一起保证"重挂载 + 换实例"同步发生, 这样工厂计数才会是 2。
   */
  const [attempt, setAttempt] = useState(0)

  return (
    <div className="mt-1 text-[12px]" data-testid="forecast-tab">
      {/* 口径行: 如实说明内嵌的是哪一页 + 标的从哪来(不预选) */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border/40 pb-2 text-[11px] text-muted-foreground">
        <span className="text-foreground">预测</span>
        <span className="text-border/60">|</span>
        <span>四模型(预测 / 历史回测 / 预测记录 / 模型权重 / 预测报告) · 惰性加载, 进入本标签才下载并取数</span>
        <span className="ml-auto text-[10px]">标的在该页搜索框内选择(预测页不接受外部标的传入, 本标签不预填)</span>
      </div>

      <div className="mt-2">
        <AppErrorBoundary
          fallback={(error, reset) => (
            <ForecastFallback
              error={error}
              onRetry={() => {
                reset()
                // 换一个**全新** lazy 实例(旧实例的 payload 已被 reject 永久污染) + 自增 key 重挂载。
                // 两件事都必须做: 少了换实例, 工厂不会重跑(React.lazy 缓存 reject); 少了换 key,
                // 重挂载语义不明确。有测试断言"重试后工厂计数 == 2"兜住这条。
                setForecastPage(lazy(importForecastPage))
                setAttempt((n) => n + 1)
              }}
            />
          )}
        >
          <Suspense
            key={attempt}
            fallback={
              <div className="flex h-[20vh] items-center justify-center text-[12px] text-muted-foreground">
                <Loader2 className="mr-2 h-3 w-3 animate-spin" />
                {LOADING_TEXT}
              </div>
            }
          >
            <ForecastPage />
          </Suspense>
        </AppErrorBoundary>
      </div>
    </div>
  )
}
