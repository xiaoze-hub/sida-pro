import { NavLink } from 'react-router-dom'
import { AlertTriangle, ChevronDown, Database, CheckCircle2 } from 'lucide-react'
import { StatusChip } from '@sida/base-ui/components/ui/status-chip'
import { Popover, PopoverContent, PopoverTrigger } from '@sida/base-ui/components/ui/popover'

import { useSourceHealth, type SourceHealthItem } from '@/hooks/useSourceHealth'
import { useVendorTrust } from '@/hooks/useVendorTrust'
import { TRUST_TONE_CLASS, formatLatency, trustTone, trustTooltip } from '@/lib/vendor-trust'

/**
 * 顶部源心跳条 (C1, 2026-09-10 借鉴 OpenTerminal tracked() 顶部展示):
 * 每源一个色段(质量分档), 悬停看 成功率/延迟 EWMA/p50/样本/最近错误; 点击进数据源页。
 * 口径: 刷新失败显式标注并沿用上次快照; 从未有样本不渲染(无源可报≠故障, 避免空条噪音)。
 */
export function SourceHeartbeat() {
  const { items, loading, error } = useVendorTrust()
  const { health: logicHealth } = useSourceHealth()

  // 兜底：如果 vendor trust 接口由于限流或初次未就绪，而 logicHealth 已有就绪源，则正常渲染心跳，不报红字
  const hasLogicReady = Object.values(logicHealth || {}).some(
    (it: unknown) => (it as SourceHealthItem)?.status === 'connected',
  )

  if (items.length === 0) {
    if (error && !loading && !hasLogicReady) {
      return (
        <div className="text-[12px] text-muted-foreground flex items-center gap-1.5" role="status">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-500 animate-pulse" />
          <span>源心跳连接中...</span>
        </div>
      )
    }
    return null
  }

  const attention = items.filter(it => ['warn', 'bad'].includes(trustTone(it.score))).length
  const unknown = items.filter(it => trustTone(it.score) === 'unknown').length
  const normal = items.filter(it => trustTone(it.score) === 'ok').length
  const allOk = !error && items.every((it) => trustTone(it.score) === 'ok')
  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="source-heartbeat">
      <Popover>
        <PopoverTrigger asChild>
          <button type="button" className="rounded-full hover:bg-s3" aria-label="查看数据源状态">
            <span className="inline-flex min-h-8 flex-wrap items-center gap-2 rounded-full border border-border bg-card px-2.5 py-1 text-[12px] text-muted-foreground">
              <Database className="h-3.5 w-3.5" aria-hidden="true" />
              <span>数据源 <b className="font-medium text-foreground">{items.length} 源</b></span>
              <span className={allOk ? 'inline-flex items-center gap-1 text-success' : ''}>{allOk && <CheckCircle2 className="h-3 w-3" />}{normal} 正常</span>
              {attention > 0 && <StatusChip tone="warning" className="min-h-6 px-2 py-0.5" data-attention="true"><AlertTriangle className="h-3 w-3" aria-hidden="true" />{attention} 需关注</StatusChip>}
              {unknown > 0 && <span>{unknown} 无样本</span>}
              <ChevronDown className="h-3 w-3" aria-hidden="true" />
            </span>
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)] space-y-3" aria-label="数据源质量详情">
          <p className="text-[14px] font-semibold">数据源质量</p>
          <p className="text-[12px] text-muted-foreground">滚动最近 100 次请求；无样本不代表可用。</p>
          <ul className="space-y-2">
            {items.map(it => <li key={it.vendor} data-vendor={it.vendor} data-tone={trustTone(it.score)} title={trustTooltip(it)} className="flex items-center justify-between gap-3 text-[12px]">
              <span className="flex items-center gap-2"><span className={`h-1.5 w-1.5 rounded-full ${TRUST_TONE_CLASS[trustTone(it.score)]}`} />{it.vendor}</span>
              <span className="text-muted-foreground">{it.samples} 次 · {it.ewma_latency_ms != null ? formatLatency(it.ewma_latency_ms) : '无延迟数据'}</span>
            </li>)}
          </ul>
          <NavLink to="/system?tab=datasources" className="inline-flex min-h-9 items-center text-[12px] font-medium text-primary">管理数据源 →</NavLink>
        </PopoverContent>
      </Popover>
      {error && <StatusChip tone="warning">刷新失败(显示上次快照)</StatusChip>}
    </div>
  )
}

export default SourceHeartbeat
