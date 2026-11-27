import type { ReactNode } from 'react'
import { cn } from '@sida/base-ui'

export type MarketDataPhase = 'loading' | 'available' | 'historical' | 'partial' | 'empty' | 'failed'

const LABEL: Record<MarketDataPhase, string> = {
  loading: '正在读取',
  available: '已载入',
  historical: '历史数据',
  partial: '盘中未定型',
  empty: '暂无数据',
  failed: '暂不可用',
}

/** Render the date and source supplied by this data set, not the page refresh time. */
export function MarketDataStatus({
  phase, date, source, subject, label, children,
}: {
  phase: MarketDataPhase
  date?: string
  source?: string
  subject: string
  label?: string
  children?: ReactNode
}) {
  const warning = phase === 'historical' || phase === 'partial' || phase === 'failed'
  return (
    <div role="status" aria-label={subject} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-muted-foreground">
      <span className={cn('rounded-md bg-s2 px-2 py-0.5 font-medium', warning && 'text-role-watch')}>{label ?? LABEL[phase]}</span>
      {date && <span>数据截至 <time dateTime={date} className="font-mono">{date}</time></span>}
      {source && <span>来源：{source}</span>}
      {children}
    </div>
  )
}
