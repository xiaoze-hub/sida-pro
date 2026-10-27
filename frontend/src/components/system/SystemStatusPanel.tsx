import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ExternalLink, Loader2, RefreshCw, Stethoscope } from 'lucide-react'
import { useTabActive } from '@sida/base-ui'
import { Button } from '@sida/base-ui/components/ui/button'
import { Switch } from '@sida/base-ui/components/ui/switch'
import { type SystemStatusNode } from '@sida/api'
import { useSystemStatus } from '@/hooks/useSystemStatus'
import { SidePanel } from '@/components/SidePanel'
import { SystemTopology, ModuleStatusChip, statusTone } from './SystemTopology'
import { MODULE_STATUS_LABELS } from '@/lib/system-status'

const TARGETS: Record<string, string> = { datasource: '/system?tab=datasources', scheduler: '/system?tab=agents', ai: '/settings?tab=settings&section=sec-ai', notify: '/settings?tab=settings&section=sec-notify' }

export function SystemStatusPanel({ enabled = true, compact = false, onNavigate }: { enabled?: boolean; compact?: boolean; onNavigate?: () => void }) {
  const active = useTabActive()
  const state = useSystemStatus(enabled && active)
  const [selected, setSelected] = useState<string | null>(null)
  const [notifySend, setNotifySend] = useState(false)
  const node = state.data?.nodes.find(item => item.id === selected)
  const failures = state.data?.nodes.filter(item => item.status === 'fail' || item.status === 'slow').length ?? 0
  if (!enabled) return <p className="rounded-xl bg-s2 p-4 text-[14px] text-muted-foreground">系统运行状态与主动检测仅管理员可查看。</p>
  return <section className="space-y-4" aria-label="系统状态模块">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-[16px] font-semibold">模块连接状态</h2><p className="mt-1 text-[12px] text-muted-foreground">{state.data.checked_at ? `${state.loading ? '上次记录' : '最近快照'} ${new Date(state.data.checked_at).toLocaleString()} · 30 秒自动更新` : '首次读取 · 尚无缓存记录'}</p></div>
      <div className="flex flex-wrap gap-2">
        {compact && <Button variant="outline" size="sm" asChild><Link to="/system/status" onClick={onNavigate}><ExternalLink className="h-4 w-4" />详细数据</Link></Button>}
        <Button variant="outline" size="sm" onClick={() => void state.refresh()} disabled={state.loading}><RefreshCw className={`h-4 w-4 ${state.loading ? 'animate-spin' : ''}`} />刷新状态</Button>
      </div>
    </div>
    {state.error && <div role="alert" className="rounded-lg bg-s2 p-3 text-[14px] text-destructive">{state.error}{state.data && '；保留上次快照，流动效果已暂停。'}</div>}
    {state.data && <>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-[12px]">
        <span className="font-medium">{state.data.nodes.length} 个模块</span><span className={failures ? 'text-destructive' : 'text-muted-foreground'}>{state.data.checked_at ? `${failures} 项需关注` : '等待读取结果'}</span>
        <ModuleStatusChip status="ok" /><ModuleStatusChip status="fail" /><ModuleStatusChip status="unknown" /><ModuleStatusChip status="unconfigured" />
      </div>
      <div className="relative" aria-busy={state.loading}>
        <SystemTopology data={state.data} selected={selected} onSelect={item => setSelected(item.id)} paused={!!state.error} pending={state.loading ? state.pendingNodes : []} />
        {state.loading && <div role="status" className="pointer-events-none absolute left-1/2 top-3 flex -translate-x-1/2 items-center gap-2 whitespace-nowrap rounded-lg bg-popover px-3 py-2 text-[12px] shadow-panel"><Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />正在更新模块 {state.refreshProgress.done}/{state.refreshProgress.total}</div>}
      </div>
      <div role="progressbar" aria-label="模块状态更新进度" aria-valuenow={state.refreshProgress.done} aria-valuemin={0} aria-valuemax={state.refreshProgress.total} className={`h-1 overflow-hidden rounded-full bg-s2 ${state.loading ? '' : 'hidden'}`}><div className="h-full bg-primary transition-[width] motion-reduce:transition-none" style={{ width: `${state.refreshProgress.total ? state.refreshProgress.done / state.refreshProgress.total * 100 : 0}%` }} /></div>
      <p className="text-[12px] leading-relaxed text-muted-foreground">连线表示模块依赖，流动表示最近检测连通，非实时请求流量。未配置、未检测、异常和待命线路不流动。数据源历史统计和通知配置校验均不视为连通。</p>
    </>}
    {!state.data && !state.loading && !state.error && <p className="rounded-xl bg-s2 p-5 text-[14px] text-muted-foreground">无状态数据，点击刷新重试。</p>}
    <details className="rounded-xl bg-s2 p-4">
      <summary className="cursor-pointer text-[14px] font-medium">主动连通性自检 {state.checking ? `· ${state.progress.done}/${state.progress.total}` : state.checkedAt ? `· ${new Date(state.checkedAt).toLocaleTimeString()}` : ''}</summary>
      <div className="mt-4 space-y-3">
        <p className="text-[12px] leading-relaxed text-muted-foreground">逐项测试数据源和平台 AI 模型；AI 检测会产生少量调用用量。通知默认只校验配置，开启下方开关才发送测试通知。探测结果最多点亮线路 60 秒。</p>
        <div className="flex flex-wrap items-center justify-between gap-3"><label className="flex items-center gap-2 text-[12px]"><Switch checked={notifySend} disabled={state.checking} onCheckedChange={setNotifySend} aria-label="自检时真实发送测试通知" />真实发送测试通知</label><Button size="sm" disabled={state.checking} onClick={() => void state.runChecks(notifySend)}><Stethoscope className="h-4 w-4" />{state.checking ? `检测 ${state.progress.done}/${state.progress.total}` : '运行连通性自检'}</Button></div>
        {state.checks.length > 0 && <ul className="divide-y divide-border">{state.checks.map(item => <li key={item.key} className="py-3 text-[12px]"><div className="flex items-center justify-between gap-3"><span className="font-medium">{item.group ? `${item.group} · ` : ''}{item.name}</span><ModuleStatusChip status={item.status} /></div>{(item.error || item.note || item.hint) && <p className={`mt-1 break-words leading-relaxed ${statusTone(item.status)}`}>{item.error || item.note} {item.hint}</p>}</li>)}</ul>}
      </div>
    </details>
    <SidePanel open={!!node} onOpenChange={open => { if (!open) setSelected(null) }} title={node?.label ?? '模块详情'} description="当前快照与连接检测依据" footer={node && TARGETS[node.id] ? <Button asChild><Link to={TARGETS[node.id]} onClick={onNavigate}>打开模块配置<ExternalLink className="h-4 w-4" /></Link></Button> : undefined}>
      {node && <ModuleDetails node={node} />}
    </SidePanel>
  </section>
}

function ModuleDetails({ node }: { node: SystemStatusNode }) {
  return <div className="space-y-4 text-[14px]"><ModuleStatusChip status={node.status} /><p className="leading-relaxed">{node.detail}</p><p className="text-[12px] text-muted-foreground">连接依据：{node.connected ? '最近探测验证通过' : '没有当前连接已验证的证据'}</p>{node.items.length > 0 && <ul className="divide-y divide-border">{node.items.map(item => <li key={item.key} className="py-3"><div className="flex justify-between gap-2"><span>{item.name}</span><span className={`text-[12px] ${statusTone(item.status)}`}>{MODULE_STATUS_LABELS[item.status]}</span></div>{(item.note || item.error || item.hint) && <p className="mt-1 break-words text-[12px] leading-relaxed text-muted-foreground">{item.error || item.note} {item.hint}</p>}</li>)}</ul>}</div>
}
