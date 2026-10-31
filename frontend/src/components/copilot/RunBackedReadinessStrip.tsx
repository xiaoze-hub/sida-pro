import { useEffect, useMemo, useRef, useState } from 'react'

import { EvidenceReadinessStrip } from '@/components/copilot/EvidenceReadinessStrip'
import type { CopilotRunStep } from '@/lib/copilot/api'
import { applyRunSteps, type ReadinessState } from '@/lib/copilot/evidence-readiness'

/**
 * 齐套度条 + 运行轨迹补全（2026-10-31）。
 *
 * 为什么需要这层：SSE 的 `stage` 只在工具**开跑**时发一条，没有完成帧，所以每条回答都挂着
 * 「已取 0/13 · 未确认 13」，正文却写满数据 —— 用户读成"13 项全没取到，这篇是编的"。
 * 服务端其实逐工具记了 `ok`（`CopilotStep.ok`，`GET /copilot/runs/{run_id}`），
 * 这里在**仍有 unknown 时**补拉一次轨迹，把能确认的条目改成 已取/失败。
 *
 * 克制之处：只在存在 unknown 时才请求（一条回答最多一次）；请求失败保持原样（不猜成功）；
 * 已有结论的条目不覆盖。
 */
export function RunBackedReadinessStrip({
  state,
  runId,
  loadDetail,
}: {
  state: ReadinessState
  runId?: string | null
  loadDetail?: (runId: string) => Promise<{ ok: boolean; data?: { steps?: CopilotRunStep[] } }>
}) {
  const [steps, setSteps] = useState<CopilotRunStep[] | null>(null)
  const loadRef = useRef(loadDetail)
  loadRef.current = loadDetail
  const needsEvidence = state.items.some((it) => it.status === 'unknown')

  useEffect(() => {
    if (!runId || !needsEvidence) return
    const load = loadRef.current
    if (!load) return
    let alive = true
    void load(runId).then((res) => {
      if (alive && res.ok && res.data?.steps) setSteps(res.data.steps)
    })
    return () => {
      alive = false
    }
  }, [runId, needsEvidence])

  const merged = useMemo(() => (steps ? applyRunSteps(state, steps) : state), [steps, state])
  if (!merged.items.length) return null
  return (
    <EvidenceReadinessStrip
      items={merged.items}
      done={merged.done}
      total={merged.total}
      failed={merged.failed}
    />
  )
}

export default RunBackedReadinessStrip
