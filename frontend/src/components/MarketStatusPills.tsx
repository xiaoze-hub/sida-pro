import type { DashboardMarketStatus } from '@panwatch/api'

/**
 * 三地市场状态徽标 (A4, 2026-09-10 借鉴 OpenTerminal):
 * 每个市场 = 名称 + 开/闭市文案(status_text, 交易中高亮) + 桌面档直显交易时段;
 * 悬停给完整时段与当地时间。口径: 后端 /stocks/markets/status 是唯一真相源
 * (CN 走交易日历含节假日; HK/US 目前仅周末口径 —— KI-012 留痕)。
 * 空列表不渲染(无状态可报≠故障)。
 */
export function MarketStatusPills({
  items,
  className = '',
}: {
  items: DashboardMarketStatus[]
  className?: string
}) {
  if (items.length === 0) return null
  return (
    <div className={`flex flex-wrap items-center gap-2 text-[11px] ${className}`.trim()}>
      {items.map((m) => (
        <span
          key={m.code}
          data-market={m.code}
          data-status={m.status}
          title={`${m.name}：${m.status_text} · 交易时段 ${m.sessions.join(' / ')} · 当地 ${m.local_time}`}
          className="inline-flex items-center gap-1.5 rounded-full bg-accent/40 px-2 py-0.5"
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${m.is_trading ? 'bg-amber-500' : 'bg-muted-foreground/40'}`}
          />
          <span className="text-muted-foreground">{m.name}</span>
          <span className={m.is_trading ? 'font-medium text-amber-600' : 'text-muted-foreground/70'}>
            {m.status_text}
          </span>
          {m.sessions.length > 0 && (
            <span className="hidden font-mono text-[10px] text-muted-foreground/60 lg:inline">
              {m.sessions.join('/')}
            </span>
          )}
        </span>
      ))}
    </div>
  )
}

export default MarketStatusPills
