import { createContext, useContext, type ReactNode } from 'react'

/**
 * 页签可见性上下文(Tab keep-alive, 2026-10-15)。
 *
 * 背景: 页签式工作台升级为**真 keep-alive** —— 每个已开页签的页面组件**同时挂载**,
 * 只是非活动页签 `display:none` 隐藏。代价是「N 页签 = N 路数据流」, 隐藏页签若继续
 * 轮询/保持 WS, 成本随页签数线性上涨。故引入本上下文: 共享数据层(轮询 hook / react-query /
 * WS 管理层)**只读一行** `const active = useTabActive()`, 隐藏时停表, 回到前台自动恢复。
 *
 * 为什么放 base-ui: 它是**零依赖底座**, `frontend/src`(页面与共享 hook)与
 * `packages/biz-ui`(K线/轮询等共享件)都能 import, 不产生回指耦合。
 *
 * 默认值 `true` —— 壳层 chrome(顶栏/心跳条/命令面板)与测试里未包 Provider 的组件一律视作
 * 「活动」, 只有真正落在隐藏页签里的组件才拿到 `false`。这样"漏接一行"的降级方向是
 * **照常工作**(不会因为上下文缺失而冻住数据), 符合"便宜的错误"原则。
 */
const TabActiveContext = createContext(true)

export function TabActiveProvider({ active, children }: { active: boolean; children: ReactNode }) {
  return <TabActiveContext.Provider value={active}>{children}</TabActiveContext.Provider>
}

/** 当前组件是否位于**活动**页签内(未包 Provider → true)。 */
export function useTabActive(): boolean {
  return useContext(TabActiveContext)
}
