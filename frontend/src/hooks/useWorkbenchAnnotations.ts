/**
 * 工作台/任一带 K 线的页面 → 服务端标注的唯一接线（2026-10-28 P2 四收口；2026-10-29 加时间点通道）。
 *
 * 页面只需要提供**精确目标**（账号 + 个股 + 代码 + 市场 + 周期），这个 hook 负责：
 *  1. 订阅账本里"当前 key"的快照（换标的/换周期 → 立刻变空，不等请求）；
 *  2. 目标变化时发一次精确读取（`annotationLink.requestAnnotations`），
 *     迟到的旧响应由账本序号丢弃；
 *  3. 把服务端行映射成**两条并列的图表图层**：
 *     - 有价位的 → 图表价位线（`price/kind/label` 逐字取自服务端行）；
 *     - 没有价位但有日粒度日期的 → 该日 K 线 marker（`annotationMarkers`）。
 *
 * 这样"画什么"与"说什么"同源：状态条读 `snapshot`，图表读 `priceLines` /
 * `annotationMarkers`，不存在图表画了但状态条不知道的第三条通道。
 *
 * 时间点标注多一步**回报闭环**（2026-10-29）：marker 能不能真的画上去取决于"该日有没有
 * K 线"，只有图表知道。所以 `reportMarkerRender` 把图表的真实结果写回账本，状态条据此
 * 区分"已上图"与"该日没有 K 线、没上图" —— 不按"服务端有行"就自称已上图。
 */

import { useCallback, useEffect, useMemo, useRef } from 'react'

import type { AnnotationMarkerDrawStatus } from '@sida/biz-ui/components/KlineChart'
import type { KlineAnnotationMarker, KlinePriceLine } from '@sida/biz-ui/klineEvents'

import { AnnotationLedger, type AnnotationSnapshot } from '@/lib/copilot/annotationLedger'
import { requestAnnotations, setCurrentAnnotationTarget } from '@/lib/copilot/annotationLink'
import { targetKey, type AnnotationTarget } from '@/lib/copilot/serverAnnotations'
import { useAnnotationSnapshot } from '@/hooks/useAnnotationSnapshot'

export interface WorkbenchAnnotations {
  snapshot: AnnotationSnapshot
  /** 图表价位线；空态为 undefined（图表不会画任何标注线） */
  priceLines: KlinePriceLine[] | undefined
  /** 图表时间点 marker；空态为 undefined（图表不会画任何标注记号） */
  annotationMarkers: KlineAnnotationMarker[] | undefined
  /** 把图表**实际**画上了哪些 marker 写回账本（交给 `KlineChart.onAnnotationMarkersDrawn`） */
  reportMarkerRender: (status: AnnotationMarkerDrawStatus) => void
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

  /**
   * 时间点 marker：**只转发账本里的服务端行**（id/动作 id/日期/标签/方向逐字取自服务端）。
   * 空态给 `undefined`（图表据此一条不画），不给 `[]` —— 与 `priceLines` 同一口径，
   * 也让"没传播注"的页面行为不变。
   */
  const annotationMarkers = useMemo<KlineAnnotationMarker[] | undefined>(
    () =>
      snapshot.markers.length > 0
        ? snapshot.markers.map((m) => ({
            annotationId: m.annotationId,
            actionId: m.actionId,
            date: m.date,
            label: m.label,
            tone: m.tone,
            note: m.note,
          }))
        : undefined,
    [snapshot.markers],
  )

  // 引用恒定：进 `KlineChart` 的 props 后不会因为父组件重渲染而反复触发 marker effect。
  const reportMarkerRender = useCallback((status: AnnotationMarkerDrawStatus) => {
    const now = ref.current
    if (!now) return
    AnnotationLedger.setMarkerRender(now, {
      drawn: status.drawn,
      skipped: status.skipped,
    })
  }, [])

  return { snapshot, priceLines, annotationMarkers, reportMarkerRender }
}

/** 测试/登出用：清空账本（生产渲染路径不要调用）。 */
export function clearAnnotationLedger() {
  AnnotationLedger.clearAll()
}
