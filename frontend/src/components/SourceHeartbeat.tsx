import { NavLink } from 'react-router-dom'

import { useSourceHealth, type SourceHealthItem } from '@/hooks/useSourceHealth'
import { useVendorTrust } from '@/hooks/useVendorTrust'
import { TRUST_TONE_CLASS, formatLatency, trustSummary, trustTone, trustTooltip } from '@/lib/vendor-trust'

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
        <div className="mb-2 text-[11px] text-muted-foreground flex items-center gap-1.5" role="status">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-500 animate-pulse" />
          <span>源心跳连接中...</span>
        </div>
      )
    }
    return null
  }

  const primary = items[0]
  // L2-E/G6(2026-10-24): 全部正常时只留一个状态圆点, 有异常(刷新失败/非 ok 档)才展开文字。
  const allOk = !error && items.every((it) => trustTone(it.score) === 'ok')
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
      {allOk && (
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" title={trustSummary(items)} />
      )}
      {/* 文字保留在 DOM(sr-only), 不卸载 —— 既有测试依赖 textContent 与 data-vendor 数量 */}
      <span className={allOk ? 'sr-only' : 'inline-flex items-center gap-2'}>
        <span>{trustSummary(items)}</span>
        {primary && primary.ewma_latency_ms != null && (
          <span className="text-muted-foreground/70">
            {primary.vendor} {formatLatency(primary.ewma_latency_ms)}
          </span>
        )}
      </span>
      {error && <span className="text-amber-600">刷新失败(显示上次快照)</span>}
    </NavLink>
  )
}

export default SourceHeartbeat
