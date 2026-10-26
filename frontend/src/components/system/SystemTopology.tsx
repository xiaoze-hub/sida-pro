import { Activity, Bell, Bot, Database, Layers, Loader2, Network, ServerCog, Timer, Waypoints } from 'lucide-react'
import { useTabActive } from '@panwatch/base-ui'
import type { ModuleStatus, SystemStatusNode, SystemStatusSnapshot } from '@panwatch/api'
import { MODULE_STATUS_LABELS } from '@/lib/system-status'

const POSITIONS: Record<string, [number, number]> = {
  database: [150, 72], biz_cache: [150, 208], redis: [150, 344], datasource: [150, 480],
  api: [500, 276], scheduler: [850, 72], ai: [850, 208], forecast_engine: [850, 344], notify: [850, 480],
}
const ICONS = { database: Database, redis: Database, cache: Layers, datasource: Activity, api: ServerCog, scheduler: Timer, ai: Bot, forecast_engine: Waypoints, notify: Bell }
export function statusTone(status: ModuleStatus) {
  return status === 'ok' ? 'text-success' : status === 'fail' ? 'text-destructive' : status === 'slow' ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'
}
export function ModuleStatusChip({ status }: { status: ModuleStatus }) {
  return <span className={`inline-flex shrink-0 items-center gap-1.5 text-[12px] ${statusTone(status)}`}><span className="h-1.5 w-1.5 rounded-full bg-current" />{MODULE_STATUS_LABELS[status]}</span>
}
function connectionPath(source: string, target: string) {
  const [sx, sy] = POSITIONS[source]
  const [tx, ty] = POSITIONS[target]
  if (sx === tx) return `M ${sx} ${sy + 46} L ${tx} ${ty - 46}`
  const direction = tx > sx ? 1 : -1
  const start = sx + direction * 102
  const end = tx - direction * 102
  const middle = (start + end) / 2
  return `M ${start} ${sy} C ${middle} ${sy}, ${middle} ${ty}, ${end} ${ty}`
}

export function SystemTopology({ data, selected, onSelect, paused = false, pending = [] }: { data: SystemStatusSnapshot; selected?: string | null; onSelect: (node: SystemStatusNode) => void; paused?: boolean; pending?: string[] }) {
  const active = useTabActive()
  const nodes = new Map(data.nodes.map(node => [node.id, node]))
  return <div className="overflow-x-auto rounded-xl bg-s2" role="region" aria-label="系统模块连接图，可左右滚动" tabIndex={0}>
    <div className="system-topology relative h-[560px] min-w-[800px]" data-testid="system-topology" data-flow-active={!paused && active ? 'true' : 'false'}>
      <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 1000 560" preserveAspectRatio="none" aria-hidden="true">
        {data.edges.filter(edge => POSITIONS[edge.source] && POSITIONS[edge.target]).map(edge => {
          const failed = nodes.get(edge.source)?.status === 'fail' || nodes.get(edge.target)?.status === 'fail'
          return <g key={edge.id} data-connection={edge.id} data-connected={edge.connected && !paused ? 'true' : 'false'}>
            <path d={connectionPath(edge.source, edge.target)} fill="none" stroke="currentColor" strokeWidth="1.5" className={failed ? 'text-destructive' : 'text-border-strong'} strokeDasharray={failed || !edge.connected ? '5 6' : undefined} />
            {edge.connected && !paused && <path d={connectionPath(edge.source, edge.target)} fill="none" stroke="currentColor" strokeWidth="2.5" strokeDasharray="5 12" className="system-connection-flow text-success" />}
          </g>
        })}
      </svg>
      {data.nodes.filter(node => POSITIONS[node.id]).map(node => {
        const [x, y] = POSITIONS[node.id]
        const Icon = ICONS[node.kind as keyof typeof ICONS] ?? Network
        return <button key={node.id} type="button" onClick={() => onSelect(node)} aria-label={`${node.label}：${MODULE_STATUS_LABELS[node.status]}，查看详情`} aria-pressed={selected === node.id} className={`card-surface absolute flex h-[92px] w-[20%] -translate-x-1/2 -translate-y-1/2 flex-col justify-center gap-2 rounded-xl px-4 text-left shadow-panel transition-colors hover:bg-s3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring ${selected === node.id ? 'bg-s3' : 'bg-card'}`} style={{ left: `${x / 10}%`, top: `${y / 5.6}%` }} data-module={node.id}>
          <span className="flex items-center gap-2"><Icon className={`h-4 w-4 shrink-0 ${node.id === 'api' ? 'text-primary' : 'text-muted-foreground'}`} /><span className="truncate text-[14px] font-semibold">{node.label}</span>{pending.includes(node.id) && <Loader2 aria-label="正在更新" className="ml-auto h-3 w-3 shrink-0 animate-spin text-muted-foreground motion-reduce:animate-none" />}</span>
          <span className="flex items-center justify-between gap-1"><ModuleStatusChip status={node.status} />{node.latency_ms !== null && <span className="font-mono text-[12px] text-muted-foreground">{node.latency_ms}ms</span>}</span>
        </button>
      })}
      <span className="pointer-events-none absolute left-1/2 top-[65%] -translate-x-1/2 text-[12px] text-muted-foreground">点击模块查看详情</span>
    </div>
  </div>
}
