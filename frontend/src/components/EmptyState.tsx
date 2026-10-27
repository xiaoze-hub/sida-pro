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
  /**
   * 空态插画插槽（2026-10-27）：放 Lottie 场景等**动效**插画，
   * 与 `icon` 的区别是它替换掉默认的圆形图标底板而不是叠加。
   */
  illustration?: ReactNode
  title: string
  description?: string
  /** 兼容旧调用（DevPageLayout 原 API 用 desc） */
  desc?: string
  action?: ReactNode
  variant?: EmptyStateVariant
  className?: string
  /** 紧凑模式：减小上下留白，用于卡片内嵌 */
  compact?: boolean
  /**
   * 口径提示(2026-09-20 空态三件套): 说明"这些数是什么口径/为什么没有"。
   * 空态必须给**原因 + 唯一主操作 + 口径**三件; 缺口径时用户会以为"没有数据",
   * 实际可能只是"这个口径没覆盖"。
   */
  caliber?: ReactNode
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
  illustration,
  title,
  description,
  desc,
  action,
  variant = 'default',
  className,
  compact = false,
  caliber,
}: EmptyStateProps) {
  const body = description ?? desc
  return (
    <div
      role={variant === 'error' ? 'alert' : 'status'}
      // data-empty-state 供布局体检断言"空态高度收敛"(禁止半屏留白) —— 规范要能被机器量到
      data-empty-state={variant}
      className={cn(
        'flex flex-col items-center justify-center rounded-xl border border-dashed border-border bg-s2 text-center',
        // 高度收敛(2026-09-20): 原先非紧凑态 py-12(≈半屏留白), 现封顶
        compact ? 'min-h-[96px] px-4 py-4' : 'min-h-[140px] px-5 py-6',
        className,
      )}
    >
      {illustration ? (
        // 有插画时替换默认图标底板：叠在一起会变成"图标套图标"，视觉噪音
        <div className="mb-2 flex items-center justify-center text-muted-foreground">{illustration}</div>
      ) : (
        <span
          className={cn(
            'mb-3 inline-flex h-12 w-12 items-center justify-center rounded-full',
            VARIANT_ICON_WRAP[variant],
          )}
        >
          {icon ?? VARIANT_ICON[variant]}
        </span>
      )}
      <p className="text-[14px] font-medium text-foreground">{title}</p>
      {body && <p className="mt-1 max-w-sm text-[12px] leading-relaxed text-muted-foreground">{body}</p>}
      {action && <div className="mt-3">{action}</div>}
      {caliber && <div className="mt-2 text-[12px] text-muted-foreground">{caliber}</div>}
    </div>
  )
}

export default EmptyState
