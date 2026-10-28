/**
 * 工作台/任一带 K 线的页面 → 服务端标注的唯一接线（2026-10-28 P2 四收口）。
 *
 * 页面只需要提供**精确目标**（账号 + 个股 + 代码 + 市场 + 周期），这个 hook 负责：
 *  1. 订阅账本里"当前 key"的快照（换标的/换周期 → 立刻变空，不等请求）；
 *  2. 目标变化时发一次精确读取（`annotationLink.requestAnnotations`），
 *     迟到的旧响应由账本序号丢弃；
 *  3. 把服务端行映射成图表价位线（`price/kind/label` 逐字取自服务端行）。
 *
 * 这样"画什么"与"说什么"同源：状态条读 `snapshot`，图表读 `priceLines`，
 * 不存在图表画了但状态条不知道的第三条通道。
 */

import { useEffect, useMemo, useRef } from 'react'

import type { KlinePriceLine } from '@sida/biz-ui/klineEvents'

import { AnnotationLedger, type AnnotationSnapshot } from '@/lib/copilot/annotationLedger'
import { requestAnnotations, setCurrentAnnotationTarget } from '@/lib/copilot/annotationLink'
import { targetKey, type AnnotationTarget } from '@/lib/copilot/serverAnnotations'
import { useAnnotationSnapshot } from '@/hooks/useAnnotationSnapshot'

export interface WorkbenchAnnotations {
  snapshot: AnnotationSnapshot
  /** 图表价位线；空态为 undefined（图表不会画任何标注线） */
  priceLines: KlinePriceLine[] | undefined
}

export function useWorkbenchAnnotations(target: AnnotationTarget | null): WorkbenchAnnotations {
  const snapshot = useAnnotationSnapshot(target)
  // 调用方可能每次渲染都新建目标对象 —— 依赖用 **key 字符串**，否则会无限发请求。
  const key = target ? targetKey(target) : ''
  const ref = useRef<AnnotationTarget | null>(target)
  ref.current = target

  useEffect(() => {
    // 视图变化即登记"当前看的是哪张图"：确认接线据此判断标的/周期是否一致，
    // 不一致时如实说"未上图"，而不是画到另一张图上。
    setCurrentAnnotationTarget(ref.current)
    if (!ref.current) return
    void requestAnnotations(ref.current)
  }, [key])

  const priceLines = useMemo<KlinePriceLine[] | undefined>(
    () =>
      snapshot.lines.length > 0
        ? snapshot.lines.map((line) => ({ price: line.price, kind: line.kind, label: line.label }))
        : undefined,
    [snapshot.lines],
  )

  return { snapshot, priceLines }
}

/** 测试/登出用：清空账本（生产渲染路径不要调用）。 */
export function clearAnnotationLedger() {
  AnnotationLedger.clearAll()
}
