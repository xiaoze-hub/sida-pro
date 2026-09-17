import type { ReactNode } from 'react'
import { AlertTriangle, WifiOff, ShieldAlert, ServerCrash, Clock, RefreshCw } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { cn } from '@/lib/utils'
import { classifyApiError, type ApiErrorKind } from '@/lib/api-error'

/**
 * 通用错误态组件（亮/暗双态主题）。
 * 支持网络 / 权限 / 服务器 / 超时 / 未知 五类错误，带图标 + 文案 + 重试按钮。
 *
 * Usage:
 *   <ErrorState error={err} onRetry={refetch} />
 *   <ErrorState type="network" title="无法连接服务器" onRetry={load} />
 */

export type ErrorStateType = 'network' | 'permission' | 'server' | 'timeout' | 'unknown'

const KIND_TO_TYPE: Record<ApiErrorKind, ErrorStateType> = {
  NETWORK: 'network',
  HTTP_4xx: 'permission',
  HTTP_5xx: 'server',
  TIMEOUT: 'timeout',
  UNKNOWN: 'unknown',
}

const TYPE_META: Record<ErrorStateType, {
  icon: ReactNode
  title: string
  hint: string
  iconWrap: string
}> = {
  network: {
    icon: <WifiOff className="h-5 w-5" />,
    title: '网络连接失败',
    hint: '请检查网络后重试',
    iconWrap: 'bg-amber-500/10 text-amber-600 dark:text-amber-500',
  },
  permission: {
    icon: <ShieldAlert className="h-5 w-5" />,
    title: '没有访问权限',
    hint: '请登录或申请相应权限后重试',
    iconWrap: 'bg-amber-500/10 text-amber-600 dark:text-amber-500',
  },
  server: {
    icon: <ServerCrash className="h-5 w-5" />,
    title: '服务暂时不可用',
    hint: '服务器开小差了，请稍后重试',
    iconWrap: 'bg-destructive/10 text-destructive',
  },
  timeout: {
    icon: <Clock className="h-5 w-5" />,
    title: '请求超时',
    hint: '数据源响应较慢，请重试或稍后再试',
    iconWrap: 'bg-amber-500/10 text-amber-600 dark:text-amber-500',
  },
  unknown: {
    icon: <AlertTriangle className="h-5 w-5" />,
    title: '加载失败',
    hint: '出现未知错误，请重试',
    iconWrap: 'bg-destructive/10 text-destructive',
  },
}

/** 从 Error / message 推断错误类型（复用 api-error 分类）。 */
export function inferErrorType(error: unknown): ErrorStateType {
  if (!error) return 'unknown'
  const status = (error as { status?: number; response?: { status?: number } })?.status
    ?? (error as { response?: { status?: number } })?.response?.status
  if (typeof status === 'number') {
    if (status === 401 || status === 403) return 'permission'
    if (status >= 500) return 'server'
    if (status === 408) return 'timeout'
  }
  return KIND_TO_TYPE[classifyApiError(error)] ?? 'unknown'
}

export interface ErrorStateProps {
  /** 传入 Error 对象或 message，自动推断类型 */
  error?: unknown
  /** 显式指定类型（优先于 error 推断） */
  type?: ErrorStateType
  title?: string
  description?: string
  onRetry?: () => void
  retryLabel?: string
  /** 额外操作区（如「返回首页」） */
  action?: ReactNode
  className?: string
  compact?: boolean
}

export function ErrorState({
  error,
  type,
  title,
  description,
  onRetry,
  retryLabel = '重试',
  action,
  className,
  compact = false,
}: ErrorStateProps) {
  const resolved: ErrorStateType = type ?? inferErrorType(error)
  const meta = TYPE_META[resolved] ?? TYPE_META.unknown
  const rawMsg = error
    ? String((error as { message?: string })?.message ?? error).slice(0, 120)
    : ''

  const displayTitle = title ?? meta.title
  const displayDesc = description ?? (rawMsg && rawMsg !== meta.title ? rawMsg : meta.hint)

  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col items-center justify-center rounded-xl border border-dashed border-destructive/30 bg-destructive/5 text-center',
        compact ? 'px-4 py-8' : 'px-6 py-12',
        className,
      )}
    >
      <span
        className={cn(
          'mb-3 inline-flex h-12 w-12 items-center justify-center rounded-full',
          meta.iconWrap,
        )}
      >
        {meta.icon}
      </span>
      <p className="text-[13px] font-medium text-foreground">{displayTitle}</p>
      <p className="mt-1 max-w-sm text-[11px] leading-relaxed text-muted-foreground">{displayDesc}</p>
      {(onRetry || action) && (
        <div className="mt-4 flex items-center gap-2">
          {onRetry && (
            <Button size="sm" variant="outline" onClick={onRetry} className="h-8 gap-1.5 px-3 text-[12px]">
              <RefreshCw className="h-3.5 w-3.5" />
              {retryLabel}
            </Button>
          )}
          {action}
        </div>
      )}
    </div>
  )
}

export default ErrorState
