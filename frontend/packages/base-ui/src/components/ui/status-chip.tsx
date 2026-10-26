import type { ComponentPropsWithoutRef } from 'react'
import { cn } from '../../cn'

/** 状态或上下文标签；交互时由外层 button/link 承担键盘语义。 */
export function StatusChip({ tone = 'neutral', className, ...props }: ComponentPropsWithoutRef<'span'> & {
  tone?: 'neutral' | 'info' | 'success' | 'warning' | 'danger'
}) {
  const tones = {
    neutral: 'bg-s2 text-muted-foreground',
    info: 'bg-primary/10 text-primary',
    success: 'bg-success/10 text-success',
    warning: 'bg-role-opp/10 text-role-opp',
    danger: 'bg-destructive/10 text-destructive',
  }
  return <span className={cn('inline-flex min-h-7 max-w-full items-center gap-1.5 rounded-full px-2.5 py-1 text-[12px] font-medium leading-4', tones[tone], className)} {...props} />
}
