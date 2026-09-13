import { useMemo, type ReactNode } from 'react'
import { InsightContext } from '@panwatch/biz-ui/components/insight/context'
import { useInsightData, type ResourceKey } from '@panwatch/biz-ui/components/insight/useInsightData'
import { useInsightDerived } from '@panwatch/biz-ui/components/insight/useInsightDerived'
import { useInsightActions } from '@panwatch/biz-ui/components/insight/useInsightActions'
import type { StockInsightModalProps } from '@panwatch/biz-ui/components/insight/types'

/** 供 Task 11–16 声明「本标签需要哪些端点」时直接 import 的类型。 */
export type { ResourceKey }

/**
 * 惰性 `InsightProvider`(工作台 v2 三合一, Task 10)。
 *
 * 背景: Task 9 从 `b49263c` 原样恢复了 `insight/` 18 个 tab 组件, 它们通过 `useInsight()`
 * 读 `InsightContext` —— 无 Provider 时 `useInsight()` **直接 throw**。旧模态壳
 * (`stock-insight-modal.tsx`, 已退役) 才是原来的 Provider, 本组件在**工作台页内**补上它,
 * **不复刻模态壳**(无 Dialog/无遮罩/无关闭, 不渲染任何自己的 DOM)。
 *
 * 惰性(spec §4.3, 两层):
 *  1. **按标签挂载** —— 本 Provider 定位是挂在每个标签内部(Task 11–16 各自包住自己的正文),
 *     `TabPanel` 只渲染当前激活标签 ⇒ 未挂载的标签完全不取数;
 *  2. **按资源键门控(`keys`)** —— 传入 `keys` 后, 只有被启用的端点才会在挂载时取数
 *     (`useInsightData` 的每个取数 effect 逐键早退), 自动 AI 建议触发(`useInsightActions`)
 *     也只在 `suggestions` 键启用时才可能发生。`keys` **省略 = 全开**, 与不传该参数时的旧
 *     行为逐字相同(默认路径不受影响)。
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
  /**
   * 资源门控键(Task 11–16 各标签按需声明)。省略 = 全开(旧行为); 空数组 = 全关。
   * 内部按**内容签名**记忆化(`keys.join(',')`), 故调用方传内联数组也不会每帧重取数。
   */
  keys?: readonly ResourceKey[]
  children: ReactNode
}

/** 模块级 noop: `onOpenChange` 身份稳定, 避免每次渲染新建函数(本组件无关闭语义)。 */
const noop = () => {}

export function InsightProvider({
  symbol,
  market,
  stockName,
  hasPosition,
  keys,
  children,
}: InsightProviderProps) {
  const props: StockInsightModalProps = { open: true, onOpenChange: noop, symbol, market, stockName, hasPosition }
  // 门控键: 按**内容签名**(非数组/Set 引用)记忆化 —— 宿主写 `keys={['core']}` 这类内联数组时
  // 每帧都是新数组, 若直接 `new Set(keys)` 会每帧换引用 ⇒ 取数 effect 反复重跑(请求风暴)。
  // 签名是原始字符串: 内容不变则 Set 引用不变; `undefined`(全开)与 `[]`(全关)用 `null` 区分。
  const keysSignature = keys === undefined ? null : keys.join(',')
  const enabledKeys = useMemo<ReadonlySet<ResourceKey> | undefined>(
    // Task 10 复审修复: 空数组的签名是 `''`(非 `null`)——**不能**走 `''.split(',')`, 那会得到
    // `['']`(size 1)⇒ `hasAnyResourceEnabled` 判真 ⇒ 20s 自动刷新 interval 照启动(与
    // "空集 = 全关"契约相悖)。`''` 必须显式映射成**真空集**(size 0), 只有 `null` 才是全开。
    () =>
      keysSignature === null
        ? undefined
        : keysSignature === ''
          ? new Set<ResourceKey>()
          : new Set(keysSignature.split(',') as ResourceKey[]),
    [keysSignature],
  )
  const data = useInsightData(props, enabledKeys)
  const derived = useInsightDerived(props, data)
  const actions = useInsightActions(props, data, derived, enabledKeys)
  return (
    <InsightContext.Provider value={{ props, ...data, ...derived, ...actions }}>
      {children}
    </InsightContext.Provider>
  )
}

export default InsightProvider
