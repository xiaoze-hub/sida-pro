import type { ReactNode } from 'react'
import { cn } from '@panwatch/base-ui'

/**
 * 统一指标格(2026-09-05 UI 全量改造 Wave1)。
 * 字体三档锁死: label 10-11px muted / value 数字(默认 tabular-nums) / sub 10px。
 * 颜色语义: tone 由调用方按涨跌/状态给, 本组件不猜颜色。
 */
export default function Stat({
  label,
  value,
  sub,
  tone,
  align = 'left',
  className,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  tone?: string
  align?: 'left' | 'right' | 'center'
  className?: string
}) {
  return (
    <div
      className={cn(
        'min-w-0',
        align === 'right' && 'text-right',
        align === 'center' && 'text-center',
        className,
      )}
    >
      <div className="truncate text-[10px] leading-tight text-muted-foreground">{label}</div>
      <div className={cn('truncate font-num text-[15px] font-semibold leading-snug tabular-nums', tone)}>
        {value}
      </div>
      {sub ? <div className="truncate text-[10px] leading-tight text-muted-foreground">{sub}</div> : null}
    </div>
  )
}
