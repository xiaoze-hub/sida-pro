/** 数据集能力矩阵 UI(2026-09-12, 借鉴 tick-stock-panel)。
 *
 * 两处复用同一份数据:
 *  - `DataCapabilities`: 数据源页顶部的完整矩阵(有问题的排前面);
 *  - `CapabilityPill`: 侧边栏常驻一行"数据能力 N/M · 降级项", 一眼知道自己在不在降级态。
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchAPI } from '@panwatch/api'
import { Layers } from 'lucide-react'
import {
  STATUS_DOT,
  STATUS_TEXT,
  capabilityRateCell,
  capabilitySummary,
  effectiveSource,
  sortItemsByRisk,
  type CapabilitiesResp,
  type CapabilityItem,
} from '@/lib/data-capabilities'

const REFRESH_MS = 5 * 60 * 1000   // 能力状态变化慢, 5 分钟一刷足够

/** 拉一次能力矩阵; 失败时保留旧值(不闪空)。 */
export function useCapabilities(): { data: CapabilitiesResp | null; loading: boolean } {
  const [data, setData] = useState<CapabilitiesResp | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await fetchAPI<CapabilitiesResp>('/datasources/capabilities')
        if (alive) setData(res)
      } catch {
        /* 保留旧数据 */
      } finally {
        if (alive) setLoading(false)
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), REFRESH_MS)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [])

  return { data, loading }
}

function Row({ item, minSamples }: { item: CapabilityItem; minSamples?: number }) {
  const eff = effectiveSource(item)
  return (
    <div className="flex items-center gap-2 py-1 text-[12px]" title={`${item.reason}${eff ? ` · 路由生效源 ${eff.provider}` : ''}`}>
      <span className={`h-2 w-2 shrink-0 rounded-full ${STATUS_DOT[item.status]}`} />
      <span className="w-[92px] shrink-0 truncate">{item.label}</span>
      <span className={`w-[52px] shrink-0 ${STATUS_TEXT[item.status]}`}>{item.status_label}</span>
      <span className="w-[54px] shrink-0 text-muted-foreground">
        {item.enabled_count}源
      </span>
      <span className="w-[74px] shrink-0 font-mono text-[11px] text-muted-foreground">
        {capabilityRateCell(item, minSamples)}
      </span>
      <span className="ml-auto shrink-0 font-mono text-[11px] text-muted-foreground">
        {item.age_days == null ? '' : `数据滞后 ${item.age_days} 天`}
      </span>
    </div>
  )
}

export function DataCapabilities() {
  const { data } = useCapabilities()
  if (!data?.items?.length) return null
  const items = sortItemsByRisk(data.items)
  const s = data.summary
  return (
    <div className="card mb-3 p-3">
      <div className="mb-1 flex items-center gap-2">
        <Layers className="h-4 w-4 text-muted-foreground" strokeWidth={1.8} />
        <span className="text-[13px] font-semibold">数据能力矩阵</span>
        <span className="text-[11px] text-muted-foreground">
          按数据集看此刻可用性(不是按源罗列); 未测量的类型标灰, 不算正常
        </span>
        <span className="ml-auto text-[11px] text-muted-foreground">
          正常 {s.ok} · 降级 {s.degraded} · 未测量 {s.unknown} · 无可用源 {s.unavailable}
        </span>
      </div>
      <div className="divide-y divide-border/40">
        {items.map((i) => <Row key={i.type} item={i} minSamples={data.min_samples} />)}
      </div>
    </div>
  )
}

/** 侧边栏一行常驻提示: 有降级/缺失时可点击跳到数据源设置。 */
export function CapabilityPill({ collapsed = false }: { collapsed?: boolean }) {
  const { data } = useCapabilities()
  const navigate = useNavigate()
  if (!data?.summary) return null
  const s = data.summary
  const bad = s.unavailable + s.degraded
  if (!bad && collapsed) return null
  return (
    <button
      type="button"
      onClick={() => navigate('/system?tab=datasources')}
      title={capabilitySummary(s) + ' —— 点击查看数据能力矩阵'}
      className="mx-2 mb-1 flex items-center gap-1.5 rounded px-1.5 py-1 text-left text-[11px] hover:bg-accent/60"
    >
      <span className={`h-2 w-2 shrink-0 rounded-full ${bad ? STATUS_DOT.degraded : STATUS_DOT.ok}`} />
      <span className="min-w-0 truncate text-muted-foreground">
        {collapsed ? `${s.ok}/${s.total}` : capabilitySummary(s)}
      </span>
    </button>
  )
}
