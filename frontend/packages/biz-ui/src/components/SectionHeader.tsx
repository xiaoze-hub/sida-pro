import type { ReactNode } from 'react'
import { cn } from '@panwatch/base-ui'

/**
 * 分区标题(2026-09-05 UI 全量改造 Wave1)。
 * 统一样式: 13px semibold + 左侧 hairline 锚点色条(去卡片化, 信息按"决策需要"分层)。
 * 全站分区标题逐页迁移到此组件, 消灭各页自写样式。
 */
export default function SectionHeader({
  title,
  action,
  className,
}: {
  title: string
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('mb-2 flex items-center justify-between gap-2', className)}>
      <div className="flex min-w-0 items-center gap-2">
        <span className="h-3.5 w-[3px] shrink-0 rounded-full bg-primary/80" aria-hidden />
        <span className="truncate text-[13px] font-semibold text-foreground">{title}</span>
      </div>
      {action ? <div className="flex shrink-0 items-center gap-1.5">{action}</div> : null}
    </div>
  )
}
