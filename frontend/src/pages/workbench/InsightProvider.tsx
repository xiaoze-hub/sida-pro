import type { ReactNode } from 'react'
import { InsightContext } from '@panwatch/biz-ui/components/insight/context'
import { useInsightData } from '@panwatch/biz-ui/components/insight/useInsightData'
import { useInsightDerived } from '@panwatch/biz-ui/components/insight/useInsightDerived'
import { useInsightActions } from '@panwatch/biz-ui/components/insight/useInsightActions'
import type { StockInsightModalProps } from '@panwatch/biz-ui/components/insight/types'

/**
 * 惰性 `InsightProvider`(工作台 v2 三合一, Task 10)。
 *
 * 背景: Task 9 从 `b49263c` 原样恢复了 `insight/` 18 个 tab 组件, 它们通过 `useInsight()`
 * 读 `InsightContext` —— 无 Provider 时 `useInsight()` **直接 throw**。旧模态壳
 * (`stock-insight-modal.tsx`, 已退役) 才是原来的 Provider, 本组件在**工作台页内**补上它,
 * **不复刻模态壳**(无 Dialog/无遮罩/无关闭, 不渲染任何自己的 DOM)。
 *
 * 惰性(关键): 本 Provider 定位是**挂在每个标签内部**(Task 11–16 各自包住自己的正文),
 * `TabPanel` 只渲染当前激活标签 ⇒ 挂载某个标签 = 才触发该标签下 `useInsightData` 的取数。
 * 因此本任务只**创建** Provider, **不做任何接线**(接线属 Task 17); `props.open` 恒 `true`
 * —— 钩子内所有取数 effect 都以 `props.open` 为总闸, 恒 `true` 才会在挂载时取数。
 *
 * 与旧模态壳的三处语义差异(均为"无壳"的必然结果, 非新增逻辑):
 *  1. `onOpenChange` 传 **noop**: 组件内 `goFullQuote()`(切换路由到 `/quote/:symbol`)与
 *     `InsightHeaderBar` 的关闭按钮原本靠它收壳; 工作台本身就是整页, 无壳可收, 路由跳转
 *     仍由 `goFullQuote` 自己完成 —— 若复用 `InsightHeaderBar`, 需 Task 11–16 复核该按钮语义;
 *  2. **不传 `useMemo` 缓存** context value: 三个钩子返回的对象字面量**每次渲染都是新引用**,
 *     `useMemo` 依赖只能写 `[symbol, market]`(会冻结首帧空数据 = 标签永远拿不到取数结果),
 *     写全依赖则每帧必然重建 —— 缓存无收益, 故直接内联展开(薄包装, 无额外逻辑);
 *  3. `market` **原样透传**(不默认 `'CN'`): 钩子内部已 `String(props.market || 'CN').toUpperCase()`。
 *
 * 依赖宿主环境(与旧模态壳一致, 非本组件引入): `useInsightData` 用 `useNavigate()`
 * ⇒ 必须挂在 Router 内; `useInsightData`/`useInsightActions` 用 `useToast()`
 * ⇒ 必须挂在 ToastProvider 内。工作台路由在 `App.tsx` 的两者之内, 天然满足。
 *
 * 归属 `src/pages/workbench/`: 属页面级装配(与 `IndexBody` 同层, 见其头注的包边界理由),
 * 不进 biz-ui —— 避免给 biz-ui 增加"必须由宿主提供 Router/Toast"的新隐性契约。
 */
export interface InsightProviderProps {
  symbol: string
  market: string
  stockName?: string
  hasPosition?: boolean
  children: ReactNode
}

/** 模块级 noop: `onOpenChange` 身份稳定, 避免每次渲染新建函数(本组件无关闭语义)。 */
const noop = () => {}

export function InsightProvider({
  symbol,
  market,
  stockName,
  hasPosition,
  children,
}: InsightProviderProps) {
  const props: StockInsightModalProps = { open: true, onOpenChange: noop, symbol, market, stockName, hasPosition }
  const data = useInsightData(props)
  const derived = useInsightDerived(props, data)
  const actions = useInsightActions(props, data, derived)
  return (
    <InsightContext.Provider value={{ props, ...data, ...derived, ...actions }}>
      {children}
    </InsightContext.Provider>
  )
}

export default InsightProvider
