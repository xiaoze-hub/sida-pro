import { useEffect, useRef, type DependencyList } from 'react'

import { useTabActive } from './tab-activity'

/**
 * 「隐藏页签停表」的 setInterval 替代品 —— 2026-10-15(keep-alive 成本闸门补全)。
 *
 * 背景: 共享数据层(`hooks/usePolling` / `hooks/useApiQuery` / `realtime/useQuoteStream`)已接
 * `useTabActive` 闸门, 但页面里**手写的裸 `window.setInterval(...)` 轮询没接** —— keep-alive 下
 * 隐藏页签只是 `display:none` 不卸载, 于是照样每 N 秒发请求, `N 页签 = N 路数据流`。
 * 本 hook 把「裸 setInterval + 首拉 + cleanup」的三段式收成一个带闸门的调用点。
 *
 * 语义 = 原 `useEffect(() => { void cb(); const t = setInterval(cb, ms); return () => clearInterval(t) }, deps)`
 * 再加页签可见性闸门:
 *   - **活动页签**: 挂载 / `deps` 变化 时立即回调一次(等价原 effect 的「首拉」), 之后每 `delayMs` 一次;
 *   - **隐藏页签**: 一律**不回调、不打表**(挂载、`deps` 变化、到点 —— 都不发请求);
 *   - **隐藏 → 回前台**: 立即回调一次(把停表期间欠下的刷新补上), 再恢复定时;
 *   - `delayMs <= 0`: 视为**停用**(不打表也不回调; 用于「用户关了自动刷新」这类开关)。
 *
 * 回调经 ref 读取 ⇒ 调用方每次渲染重造箭头函数**不会**重装定时器、也不触发额外请求。
 *
 * 与共享层其余闸门的分工/口径一致性: `useApiQuery` 的口径是「隐藏仍完成**初次**取数, 只停轮询」;
 * 本 hook 更严 —— 隐藏时**连首拉也不做**(挂载在隐藏页签里的页面一个请求都不发), 与 `usePolling` 同口径。
 * 这正是页面级轮询想要的: 重开工作台时, 被恢复出来的隐藏页签不该先打一轮再停。
 *
 * @param callback 轮询体(与原 `setInterval` 的第一个参数同义; 这次调用的 req 叠加/错误处理仍归调用方)
 * @param delayMs  周期 ms; `<= 0` 表示停用
 * @param opts.deps 变化即「重挂」(立即补一次 + 重新计时), 等价原 effect 的依赖数组
 * @param opts.immediate 挂载 / `deps` 变化时是否立即回调一次, 默认 `true`
 *   (原 `setInterval` 不配首拉时置 `false`; 「隐藏 → 回前台补一次」不受此开关影响, 始终保留)
 */
export interface UsePausedIntervalOptions {
  /** 依赖数组: 变化即立即补一次并重新计时(等价原 effect 重跑)。 */
  deps?: DependencyList
  /** 挂载 / 依赖变化时是否立即回调一次, 默认 true。 */
  immediate?: boolean
}

export function usePausedInterval(
  callback: () => void,
  delayMs: number,
  { deps = [], immediate = true }: UsePausedIntervalOptions = {},
): void {
  const tabActive = useTabActive()

  // 最新回调放 ref: 回调身份变化不该影响定时器。
  const cbRef = useRef(callback)
  useEffect(() => {
    cbRef.current = callback
  })

  // 上一次是否「在跑」: `null`=尚未跑过(挂载) / `false`=停着(隐藏或停用) / `true`=在跑。
  // 只有 `false → true` 才是「隐藏后回前台/从停用恢复」⇒ 必须补一次;
  // 「首次挂载」不算恢复(是否补一次交给 `immediate`)。
  const wasLiveRef = useRef<boolean | null>(null)

  const live = tabActive && typeof delayMs === 'number' && delayMs > 0

  useEffect(() => {
    if (!live) {
      // 隐藏 / 停用: 停表, 并记账「下次转活要补一次」。
      wasLiveRef.current = false
      return
    }
    const resumed = wasLiveRef.current === false
    wasLiveRef.current = true
    if (immediate || resumed) cbRef.current()
    const timer = window.setInterval(() => cbRef.current(), delayMs)
    return () => window.clearInterval(timer)
    // `deps` 由调用方给(等价原 effect 依赖); 长度恒定, 展开安全。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabActive, delayMs, live, immediate, ...deps])
}
