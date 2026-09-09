import type { SchedulePreview } from './shared'
import { Badge } from '@panwatch/base-ui/components/ui/badge'
import { Bell } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Clock } from 'lucide-react'
import { Cpu } from 'lucide-react'
import { Dialog } from '@panwatch/base-ui/components/ui/dialog'
import { DialogContent } from '@panwatch/base-ui/components/ui/dialog'
import { DialogDescription } from '@panwatch/base-ui/components/ui/dialog'
import { DialogHeader } from '@panwatch/base-ui/components/ui/dialog'
import { DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Play } from 'lucide-react'
import { Select } from '@panwatch/base-ui/components/ui/select'
import { SelectContent } from '@panwatch/base-ui/components/ui/select'
import { SelectGroup } from '@panwatch/base-ui/components/ui/select'
import { SelectItem } from '@panwatch/base-ui/components/ui/select'
import { SelectLabel } from '@panwatch/base-ui/components/ui/select'
import { SelectTrigger } from '@panwatch/base-ui/components/ui/select'
import { SelectValue } from '@panwatch/base-ui/components/ui/select'
import { Switch } from '@panwatch/base-ui/components/ui/switch'
import { useStocks } from './context'

export function AgentDialogs() {
  const {
    agents,
    services,
    channels,
    agentDialogStock,
    setAgentDialogStock,
    triggeringAgent,
    schedulePreviewCache,
    schedulePreviewLoading,
    agentResultDialog,
    setAgentResultDialog,
    formatPreviewTime,
    effectiveSchedule,
    toggleAgent,
    triggerStockAgent,
    updateStockAgentModel,
    toggleStockAgentChannel,
    updateStockAgentSchedule,
  } = useStocks()
  return (
    <>
{/* Agent Assignment Dialog */}
<Dialog open={!!agentDialogStock} onOpenChange={open => !open && setAgentDialogStock(null)}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>配置监控 Agent</DialogTitle>
      <DialogDescription>
        为 {agentDialogStock?.name}（{agentDialogStock?.symbol}）选择要监控的 Agent
      </DialogDescription>
    </DialogHeader>
    <div className="space-y-3 mt-2">
      {agents.length === 0 ? (
        <p className="text-[13px] text-muted-foreground py-4 text-center">暂无可用 Agent</p>
      ) : (
        agents.map(agent => {
          const stockAgent = agentDialogStock?.agents?.find(a => a.agent_name === agent.name)
          const isAssigned = !!stockAgent
          const isBatchMode = agent.execution_mode === 'batch'
          return (
            <div key={agent.name} className="border-b border-border/40 hover:bg-accent/20 transition-colors overflow-hidden">
              <div className="flex items-center justify-between p-3.5">
                <div className="flex items-center gap-3">
                  <div className={`w-2 h-2 rounded-full ${agent.enabled ? 'bg-emerald-500' : 'bg-border'}`} />
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] font-medium text-foreground">{agent.display_name}</span>
                      <Badge variant="secondary" className="text-[9px]">
                        {isBatchMode ? '批量' : '逐只'}
                      </Badge>
                    </div>
                    <p className="text-[11px] text-muted-foreground mt-0.5">{agent.description}</p>
                  </div>
                </div>
                <Switch
                  checked={isAssigned}
                  onCheckedChange={() => agentDialogStock && toggleAgent(agentDialogStock, agent.name)}
                  disabled={!agent.enabled}
                />
              </div>
              {isAssigned && isBatchMode && (
                <div className="px-3.5 pb-3.5 pt-0">
                  <p className="text-[11px] text-muted-foreground">
                    调度、AI模型、通知渠道请在 <a href="/system?tab=agents" className="text-primary hover:underline">Agent 配置</a> 页面统一设置
                  </p>
                </div>
              )}
              {isAssigned && !isBatchMode && (
                <div className="px-3.5 pb-3.5 pt-0 space-y-2.5">
                  {/* Schedule/Interval Select */}
                  <div className="flex items-center gap-2">
                    <Clock className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
                    <Select
                      value={stockAgent?.schedule || '__default__'}
                      onValueChange={val => agentDialogStock && updateStockAgentSchedule(agentDialogStock, agent.name, val === '__default__' ? '' : val)}
                    >
                      <SelectTrigger className="h-7 text-[11px] w-auto min-w-[140px] px-2.5 bg-accent/50 border-border/50">
                        <SelectValue placeholder="执行间隔" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__default__">跟随全局</SelectItem>
                        <SelectItem value="*/1 9-15 * * 1-5">每 1 分钟</SelectItem>
                        <SelectItem value="*/3 9-15 * * 1-5">每 3 分钟</SelectItem>
                        <SelectItem value="*/5 9-15 * * 1-5">每 5 分钟</SelectItem>
                        <SelectItem value="*/10 9-15 * * 1-5">每 10 分钟</SelectItem>
                        <SelectItem value="*/15 9-15 * * 1-5">每 15 分钟</SelectItem>
                        <SelectItem value="*/30 9-15 * * 1-5">每 30 分钟</SelectItem>
                      </SelectContent>
                    </Select>
                    <span className="text-[10px] text-muted-foreground">交易时段</span>
                  </div>

                  {/* Schedule Preview */}
                  {(() => {
                    const eff = effectiveSchedule(agent, stockAgent)
                    const isFollowingGlobal = !(stockAgent?.schedule || '').trim() && !!(agent.schedule || '').trim()
                    const preview = eff ? schedulePreviewCache[eff] : null
                    const isLoading = eff ? !!schedulePreviewLoading[eff] : false
                    if (!eff) return null
                    return (
                      <div className="ml-[22px] rounded-md border border-border/40 bg-background/30 px-2.5 py-2">
                        <div className="flex items-center justify-between">
                          <div className="text-[11px] text-muted-foreground">
                            未来触发时间预览{isFollowingGlobal ? <span className="ml-1 opacity-70">(跟随全局)</span> : null}
                          </div>
                          {isLoading && (
                            <span className="w-3 h-3 border-2 border-primary/30 border-t-primary rounded-full animate-spin" />
                          )}
                        </div>
                        {'error' in (preview || {}) ? (
                          <div className="mt-1 text-[11px] text-muted-foreground">{(preview as any).error}</div>
                        ) : (preview as SchedulePreview | undefined)?.next_runs?.length ? (
                          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
                            {(preview as SchedulePreview).next_runs.map((t, i) => (
                              <span key={i} className="px-1.5 py-0.5 rounded border border-border/60 bg-accent/20 font-mono" title={t}>
                                {formatPreviewTime(t, (preview as SchedulePreview).timezone)}
                              </span>
                            ))}
                            {(preview as SchedulePreview).timezone ? (
                              <span className="opacity-60">({(preview as SchedulePreview).timezone})</span>
                            ) : null}
                          </div>
                        ) : (
                          <div className="mt-1 text-[11px] text-muted-foreground">—</div>
                        )}
                        <div className="mt-1 text-[10px] text-muted-foreground/70 font-mono">schedule: {eff}</div>
                      </div>
                    )
                  })()}

                  {/* AI Model Select */}
                  <div className="flex items-center gap-2">
                    <Cpu className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
                    <Select
                      value={stockAgent?.ai_model_id?.toString() ?? '__default__'}
                      onValueChange={val => agentDialogStock && updateStockAgentModel(agentDialogStock, agent.name, val === '__default__' ? null : parseInt(val))}
                    >
                      <SelectTrigger className="h-7 text-[11px] w-auto min-w-[140px] px-2.5 bg-accent/50 border-border/50">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__default__">系统默认</SelectItem>
                        {services.map(svc => (
                          <SelectGroup key={svc.id}>
                            <SelectLabel>{svc.name}</SelectLabel>
                            {svc.models.map(m => (
                              <SelectItem key={m.id} value={m.id.toString()}>
                                {m.name}{m.name !== m.model ? ` (${m.model})` : ''}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  {/* Notification Channels */}
                  {channels.length > 0 && (
                    <div className="flex items-center gap-2 flex-wrap">
                      <Bell className="w-3.5 h-3.5 text-muted-foreground flex-shrink-0" />
                      {channels.map(ch => {
                        const isSelected = (stockAgent?.notify_channel_ids || []).includes(ch.id)
                        return (
                          <button
                            key={ch.id}
                            onClick={() => agentDialogStock && toggleStockAgentChannel(agentDialogStock, agent.name, ch.id)}
                            className={`text-[10px] px-2 py-0.5 rounded-md border transition-colors ${
                              isSelected
                                ? 'bg-primary/10 border-primary/30 text-primary font-medium'
                                : 'bg-accent/30 border-border/50 text-muted-foreground hover:border-primary/30'
                            }`}
                          >
                            {ch.name}
                          </button>
                        )
                      })}
                      {(stockAgent?.notify_channel_ids || []).length === 0 && (
                        <span className="text-[10px] text-muted-foreground">系统默认</span>
                      )}
                    </div>
                  )}
                  {/* Trigger Button */}
                  <div className="flex items-center gap-2 pt-1">
                    <Button
                      variant="secondary" size="sm" className="h-7 text-[11px] px-2.5"
                      disabled={triggeringAgent === agent.name}
                      onClick={() => agentDialogStock && triggerStockAgent(agentDialogStock.id, agent.name)}
                    >
                      {triggeringAgent === agent.name ? (
                        <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                      ) : (
                        <Play className="w-3 h-3" />
                      )}
                      立即分析
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )
        })
      )}
    </div>
  </DialogContent>
</Dialog>

{/* Agent 分析结果弹窗 */}
<Dialog open={!!agentResultDialog} onOpenChange={open => !open && setAgentResultDialog(null)}>
  <DialogContent className="max-w-md">
    <DialogHeader>
      <DialogTitle className="text-base">{agentResultDialog?.title}</DialogTitle>
      <DialogDescription className="flex items-center gap-2 pt-1">
        {agentResultDialog?.should_alert ? (
          <Badge variant="default" className="text-[10px]">建议关注</Badge>
        ) : (
          <Badge variant="secondary" className="text-[10px]">无需关注</Badge>
        )}
        {agentResultDialog?.notified && (
          <Badge variant="outline" className="text-[10px]">已发送通知</Badge>
        )}
      </DialogDescription>
    </DialogHeader>
    <div className="mt-2 p-3 bg-accent/30 rounded-md">
      <pre className="text-[13px] whitespace-pre-wrap font-sans leading-relaxed">
        {agentResultDialog?.content}
      </pre>
    </div>
    <div className="flex justify-end mt-2">
      <Button variant="outline" size="sm" onClick={() => setAgentResultDialog(null)}>
        关闭
      </Button>
    </div>
  </DialogContent>
</Dialog>
    </>
  )
}
