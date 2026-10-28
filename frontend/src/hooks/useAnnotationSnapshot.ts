/**
 * 订阅「当前精确目标的标注快照」（2026-10-28 P2 四收口）。
 *
 * 为什么用 `useSyncExternalStore` 而不是 `useState` + 事件：
 *  - 快照按 `账号|标的类型|代码|市场|周期` 分键存在 `annotationLedger` 里，
 *    **换标的/换周期的那一刻**读到的就是新 key 的空快照 —— 旧价线同步消失，
 *    不用等 setState、也不会出现"上一只票的线还在图上"的一帧；
 *  - 迟到的旧响应由账本的发布序号丢弃，不会覆盖新目标。
 */

import { useCallback, useRef, useSyncExternalStore } from 'react'

import { AnnotationLedger, type AnnotationSnapshot } from '@/lib/copilot/annotationLedger'
import type { AnnotationTarget } from '@/lib/copilot/serverAnnotations'

export function useAnnotationSnapshot(target: AnnotationTarget | null): AnnotationSnapshot {
  // 每次渲染都更新最新目标；回调身份保持稳定（否则会反复重订阅）
  const ref = useRef<AnnotationTarget | null>(target)
  ref.current = target

  const subscribe = useCallback(
    (onChange: () => void) => AnnotationLedger.subscribe(onChange),
    [],
  )
  const getSnapshot = useCallback(() => AnnotationLedger.snapshot(ref.current), [])
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
