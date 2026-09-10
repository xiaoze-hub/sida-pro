import { NavLink } from 'react-router-dom'

import { useVendorTrust } from '@/hooks/useVendorTrust'
import { TRUST_TONE_CLASS, formatLatency, trustSummary, trustTone, trustTooltip } from '@/lib/vendor-trust'

/**
 * 顶部源心跳条 (C1, 2026-09-10 借鉴 OpenTerminal tracked() 顶部展示):
 * 每源一个色段(质量分档), 悬停看 成功率/延迟 EWMA/p50/样本/最近错误; 点击进数据源页。
 * 口径: 刷新失败显式标注并沿用上次快照; 从未有样本不渲染(无源可报≠故障, 避免空条噪音)。
 */
export function SourceHeartbeat() {
  const { items, loading, error } = useVendorTrust()

  if (items.length === 0) {
    if (error && !loading) {
      return (
        <div className="mb-3 text-[11px] text-amber-600" role="status">
          源心跳不可用(质量接口请求失败)
        </div>
      )
    }
    return null
  }

  const primary = items[0]
  return (
    <NavLink
      to="/system?tab=datasources"
      className="mb-2.5 flex items-center gap-2 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
      title="数据源质量(滚动最近 100 次) — 点击进入数据源页"
      data-testid="source-heartbeat"
    >
      <span className="shrink-0 text-muted-foreground/70">源心跳</span>
      <span className="flex items-center gap-[3px]" role="img" aria-label={trustSummary(items)}>
        {items.map((it) => (
          <span
            key={it.vendor}
            data-vendor={it.vendor}
            data-tone={trustTone(it.score)}
            className={`h-1.5 w-5 rounded-full ${TRUST_TONE_CLASS[trustTone(it.score)]}`}
            title={trustTooltip(it)}
          />
        ))}
      </span>
      <span>{trustSummary(items)}</span>
      {primary && primary.ewma_latency_ms != null && (
        <span className="text-muted-foreground/70">
          {primary.vendor} {formatLatency(primary.ewma_latency_ms)}
        </span>
      )}
      {error && <span className="text-amber-600">刷新失败(显示上次快照)</span>}
    </NavLink>
  )
}

export default SourceHeartbeat
