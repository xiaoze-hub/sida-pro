import type { DashboardMarketStatus } from '@panwatch/api'
import { ChevronDown } from 'lucide-react'
import { StatusChip } from '@panwatch/base-ui/components/ui/status-chip'
import { Popover, PopoverContent, PopoverTrigger } from '@panwatch/base-ui/components/ui/popover'

/**
 * 三地市场状态徽标 (A4, 2026-09-10 借鉴 OpenTerminal):
 * 每个市场 = 名称 + 开/闭市文案；交易时段与当地时间点击展开，避免挤占标题。
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
    <div className={`flex flex-wrap items-center gap-2 text-[12px] ${className}`.trim()}>
      {items.map((m) => (
        <Popover key={m.code}>
        <PopoverTrigger asChild>
        <button type="button"
          aria-label={`${m.name} ${m.status_text}，查看交易时段`}
          data-market={m.code}
          data-status={m.status}
          title={`${m.name}：${m.status_text} · 交易时段 ${m.sessions.join(' / ')} · 当地 ${m.local_time}`}
          className="rounded-full transition-colors hover:bg-s3 focus-visible:ring-2 focus-visible:ring-ring"
        >
          <StatusChip tone={m.is_trading ? 'warning' : 'neutral'}>
          <span
            className={`h-1.5 w-1.5 rounded-full ${m.is_trading ? 'bg-amber-500' : 'bg-muted-foreground/40'}`}
          />
          <span className="text-muted-foreground">{m.name}</span>
          <span className={m.is_trading ? 'font-medium text-role-opp' : 'text-muted-foreground'}>
            {m.status_text}
          </span>
          <ChevronDown className="h-3 w-3 opacity-60" aria-hidden="true" />
          </StatusChip>
        </button>
        </PopoverTrigger>
        <PopoverContent align="start" className="max-w-[calc(100vw-2rem)] space-y-3 text-[12px]" aria-label={`${m.name}交易时段`}>
          <p className="text-[14px] font-semibold">{m.name} · {m.status_text}</p>
          <dl className="space-y-2">
            <div className="flex justify-between gap-4"><dt className="text-muted-foreground">交易时段</dt><dd className="text-right font-mono">{m.sessions.length ? m.sessions.map(s => <div key={s}>{s}</div>) : '无数据'}</dd></div>
            <div className="flex justify-between gap-4"><dt className="text-muted-foreground">当地时间</dt><dd className="font-mono">{m.local_time || '无数据'}</dd></div>
          </dl>
        </PopoverContent>
        </Popover>
      ))}
    </div>
  )
}

export default MarketStatusPills
