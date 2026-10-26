import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Activity, ArrowUpRight } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@panwatch/base-ui/components/ui/popover'
import { useSystemStatus } from '@/hooks/useSystemStatus'
import { getJwtRole } from '@/lib/jwt'
import { ModuleStatusChip } from './SystemTopology'

const CORE = ['api', 'database', 'redis', 'biz_cache']
export function SystemStatusIndicator() {
  const owner = getJwtRole() === 'owner'
  const state = useSystemStatus(owner)
  const [open, setOpen] = useState(false)
  if (!owner) return null
  const core = state.data.nodes.filter(node => CORE.includes(node.id))
  const pending = !state.data.checked_at || core.some(node => state.pendingNodes.includes(node.id)) && state.loading
  const status = state.error ? 'slow' : pending ? 'unknown' : core.some(node => node.status === 'fail') ? 'fail' : core.some(node => node.status === 'slow') ? 'slow' : core.every(node => node.status === 'ok') ? 'ok' : 'unknown'
  const label = status === 'ok' ? '核心正常' : status === 'fail' ? '核心异常' : status === 'slow' ? '需要关注' : '待更新'
  const color = status === 'ok' ? 'text-success' : status === 'fail' ? 'text-destructive' : status === 'slow' ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground'
  return <Popover open={open} onOpenChange={setOpen}>
    <PopoverTrigger asChild><Button variant="ghost" size="icon" className={`relative ${color}`} aria-label={`系统状态：${label}`} title={`系统状态：${label}`} data-testid="system-status-indicator"><Activity className="h-4 w-4" /><span aria-hidden="true" className="absolute bottom-1.5 right-1.5 h-1.5 w-1.5 rounded-full bg-current" /></Button></PopoverTrigger>
    <PopoverContent aria-label="系统状态概览" align="start" className="w-80 max-w-[calc(100vw-2rem)] p-4">
      <h2 className="text-[14px] font-semibold">系统状态 · {label}</h2>
      <p className="mt-1 text-[12px] text-muted-foreground">{state.data.checked_at ? new Date(state.data.checked_at).toLocaleString() : '尚无状态记录'}{state.loading ? ` · 更新 ${state.refreshProgress.done}/${state.refreshProgress.total}` : ''}</p>
      <ul className="my-3 divide-y divide-border">{core.map(node => <li key={node.id} className="flex items-center justify-between py-2 text-[12px]"><span>{node.label}</span><ModuleStatusChip status={state.loading && state.pendingNodes.includes(node.id) ? 'checking' : node.status} /></li>)}</ul>
      {state.error && <p role="alert" className="mb-3 text-[12px] text-destructive">更新失败，显示上次记录。</p>}
      <Button variant="outline" size="sm" className="w-full" asChild><Link to="/system/status" onClick={() => setOpen(false)}>系统自检 · 详细数据<ArrowUpRight className="h-4 w-4" /></Link></Button>
    </PopoverContent>
  </Popover>
}
