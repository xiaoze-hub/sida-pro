import { Loader2, ShieldAlert } from 'lucide-react'
import { cn } from '@panwatch/base-ui'

/**
 * 统一错误展示组件 + 技术错误翻译 (2026-08-21)
 *
 * 解决"数据源挂时直接甩 HTTP 502 / 原始 error.message"的粗糙体验:
 * 把技术性错误翻译成人话, 圆角卡片 + 警示图标 + 友好文案 + 可选重试按钮。
 */

/**
 * 把技术性错误翻译成用户能看懂的话(纯函数, 供任意处复用)。
 * 匹配规则:
 *   - 502 / Bad Gateway / 503 / timeout        -> 行情服务开小差了
 *   - 401 / 403                                -> 登录已过期
 *   - Network Error / fetch / Failed to fetch  -> 网络连接不稳定
 *   - 其他                                     -> 原文去空白, 截断 80 字
 */
export function friendlyHttpMessage(err: unknown): string {
  const raw =
    typeof err === 'string'
      ? err
      : err instanceof Error
        ? err.message
        : ''
  const text = String(raw ?? '').trim()

  if (/502|Bad Gateway|503|timeout|timed out/i.test(text)) {
    return '行情服务开小差了, 通常几分钟内自动恢复'
  }
  if (/401|403/.test(text)) {
    return '登录已过期'
  }
  if (/Network Error|fetch|Failed to fetch/i.test(text)) {
    return '网络连接不稳定'
  }
  if (!text) {
    return ''
  }
  return text.length > 80 ? `${text.slice(0, 80)}…` : text
}

export interface ErrorStateProps {
  /** 主标题, 默认 "数据加载失败" */
  title?: string
  /** 副文案(muted 小字), 建议传 friendlyHttpMessage(err) 的结果 */
  message?: string
  /** 传入则显示"重试"按钮 */
  onRetry?: () => void
  /** 重试进行中: 按钮转圈并禁用 */
  retrying?: boolean
  className?: string
}

export default function ErrorState({
  title = '数据加载失败',
  message,
  onRetry,
  retrying,
  className,
}: ErrorStateProps) {
  const display = message ? friendlyHttpMessage(message) : ''
  return (
    <div className={cn('rounded-xl border border-border/50 bg-card p-3', className)}>
      <div className="flex items-center gap-2">
        <ShieldAlert className="w-4 h-4 shrink-0 text-muted-foreground" />
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-semibold text-foreground">{title}</div>
          {display ? (
            <div className="mt-0.5 break-words text-[11px] text-muted-foreground">{display}</div>
          ) : null}
        </div>
        {onRetry ? (
          <button
            type="button"
            disabled={retrying}
            onClick={onRetry}
            className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-lg border border-border bg-secondary px-2.5 text-[12px] font-medium text-foreground transition-colors hover:bg-secondary/80 disabled:pointer-events-none disabled:opacity-50"
          >
            {retrying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            重试
          </button>
        ) : null}
      </div>
    </div>
  )
}
