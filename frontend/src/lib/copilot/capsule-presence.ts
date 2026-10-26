/**
 * 参谋胶囊「在场」登记表（2026-10-18）。
 *
 * 为什么需要它：个股工作台右下角同时有 ChatWidget 的圆形 FAB 与 CopilotCapsule 胶囊，
 * 两个入口叠在一起既重复又互相遮挡。胶囊是**带当前标的上下文的更高级入口**，有它时
 * 全局 FAB 就该让位；但 FAB 在其它页面仍是唯一入口，不能一刀切删掉。
 *
 * 用模块级计数 + `useSyncExternalStore`（而不是 Context）：ChatWidget 挂在 App 根部，
 * 胶囊挂在工作台深层，Context 需要穿透整棵树；计数表零 prop drilling、无 Provider 顺序问题，
 * 且多个胶囊同时挂载（理论上不会，但 keep-alive 可能短暂并存）也不会提前隐藏 FAB。
 */
import { useSyncExternalStore } from 'react'

let mounted = 0
const listeners = new Set<() => void>()

function emit() {
  for (const fn of listeners) fn()
}

/** 胶囊挂载时调用；返回注销函数（useEffect cleanup 直接返回它）。 */
export function registerCapsulePresence(): () => void {
  mounted += 1
  emit()
  let released = false
  return () => {
    if (released) return // StrictMode 双调用/重复 cleanup 不重复扣减
    released = true
    mounted = Math.max(0, mounted - 1)
    emit()
  }
}

/** 当前是否已有胶囊在场（ChatWidget 据此隐藏右下角 FAB）。 */
export function useCapsulePresent(): boolean {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb)
      return () => listeners.delete(cb)
    },
    () => mounted > 0,
    () => false, // SSR/首屏快照: 保守返回 false，FAB 不闪
  )
}

/** 仅测试用：清空计数（避免用例间串态）。 */
export function resetCapsulePresenceForTest(): void {
  mounted = 0
  emit()
}
