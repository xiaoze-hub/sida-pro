import type { ReactNode } from 'react'
import { Inbox, AlertCircle, Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * 通用空态组件（亮/暗双态主题）。
 * - default: 列表/目录为空
 * - error:   加载失败（也可用 ErrorState）
 * - loading: 加载中占位
 *
 * 风格与现有 UI 一致：虚线边框 + 圆图标 + 居中文案 + 可选操作按钮。
 */
export type EmptyStateVariant = 'default' | 'error' | 'loading'

export interface EmptyStateProps {
  icon?: ReactNode
  title: string
  description?: string
  /** 兼容旧调用（DevPageLayout 原 API 用 desc） */
  desc?: string
  action?: ReactNode
  variant?: EmptyStateVariant
  className?: string
  /** 紧凑模式：减小上下留白，用于卡片内嵌 */
  compact?: boolean
}

const VARIANT_ICON: Record<EmptyStateVariant, ReactNode> = {
  default: <Inbox className="h-6 w-6" />,
  error: <AlertCircle className="h-6 w-6" />,
  loading: <Loader2 className="h-6 w-6 animate-spin" />,
}

const VARIANT_ICON_WRAP: Record<EmptyStateVariant, string> = {
  default: 'bg-muted text-muted-foreground',
  error: 'bg-destructive/10 text-destructive',
  loading: 'bg-primary/10 text-primary',
}

export function EmptyState({
  icon,
  title,
  description,
  desc,
  action,
  variant = 'default',
  className,
  compact = false,
}: EmptyStateProps) {
  const body = description ?? desc
  return (
    <div
      role={variant === 'error' ? 'alert' : 'status'}
      className={cn(
        'flex flex-col items-center justify-center rounded-xl border border-dashed border-border/70 bg-muted/20 text-center',
        compact ? 'px-4 py-8' : 'px-6 py-12',
        className,
      )}
    >
      <span
        className={cn(
          'mb-3 inline-flex h-12 w-12 items-center justify-center rounded-full',
          VARIANT_ICON_WRAP[variant],
        )}
      >
        {icon ?? VARIANT_ICON[variant]}
      </span>
      <p className="text-[13px] font-medium text-foreground">{title}</p>
      {body && <p className="mt-1 max-w-sm text-[11px] leading-relaxed text-muted-foreground">{body}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export default EmptyState
