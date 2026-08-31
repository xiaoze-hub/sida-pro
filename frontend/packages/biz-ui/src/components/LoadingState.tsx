import { Loader2 } from 'lucide-react'
import { cn } from '@panwatch/base-ui'

/**
 * 统一加载态组件 (2026-08-21)
 * skeleton 模式: rows 个圆角脉冲条(参考 DarkFlowCards 原有写法)。
 * spinner 模式: 居中转圈 + 可选 label。
 */

export interface LoadingStateProps {
  variant?: 'skeleton' | 'spinner'
  /** skeleton 模式的占位条数量 */
  rows?: number
  /** 可选说明文案 */
  label?: string
  className?: string
}

export default function LoadingState({
  variant = 'skeleton',
  rows = 3,
  label,
  className,
}: LoadingStateProps) {
  if (variant === 'spinner') {
    return (
      <div
        className={cn(
          'flex flex-col items-center justify-center gap-2 py-6 text-muted-foreground',
          className,
        )}
      >
        <Loader2 className="h-5 w-5 animate-spin" />
        {label ? <div className="text-[12px]">{label}</div> : null}
      </div>
    )
  }

  return (
    <div className={cn('grid gap-2', className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="h-[92px] animate-pulse rounded-xl border border-border/50 bg-card bg-accent/20"
        />
      ))}
      {label ? <div className="text-[11px] text-muted-foreground">{label}</div> : null}
    </div>
  )
}
