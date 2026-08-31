/**
 * Data source failure explicit banner (2026-08-17 v0.2.64)
 *
 * Closes B review P1-5 + P1-6:
 *   - Same-source 失败去重 (pushError 不再累积同一 source)
 *   - onDismiss 按 stable id 而非 array index
 *   - 超过 MAX_DISPLAY 条数显示 "还有 N 个源失败"
 *
 * Usage:
 *   const [errors, setErrors] = useState<SourceError[]>([])
 *   ...
 *   api.x().catch((e) => {
 *     setErrors(prev => [...prev, { id: nanoid(), source: '同花顺资金流', message: String(e), retry: load }])
 *   })
 *   ...
 *   <ErrorBanner errors={errors} onDismiss={(id) => setErrors(prev => prev.filter(e => e.id !== id))} />
 */

import { useEffect, useState } from 'react'
import { AlertTriangle, X, RotateCw } from 'lucide-react'

let nextErrorId = 0
export function makeErrorId(): number {
  return ++nextErrorId
}

export interface SourceError {
  /** stable id — 用 makeErrorId() 生成, dismiss 按此移除 */
  id?: number  // 2026-08-17 v0.2.64: 可选 — 旧代码 path 仍用, 自动 fallback 到 idx
  /** 数据源/服务名, 用于展示 */
  source: string
  /** 错误消息(简短) */
  message: string
  /** 重试回调(可选) */
  retry?: () => void
  /** 自动消失毫秒(0 = 不自动消失) */
  auto_dismiss_ms?: number
}

interface ErrorBannerProps {
  errors: SourceError[]
  /** 按 id 移除 (B 报告 P1-6 修复) */
  onDismiss?: (id: number) => void
  /** 是否允许一键重试全部 */
  retryAll?: () => void
  /** 超过此数折叠显示(默认 3) */
  maxDisplay?: number
}

const DEFAULT_MAX_DISPLAY = 3

export function ErrorBanner({ errors, onDismiss, retryAll, maxDisplay = DEFAULT_MAX_DISPLAY }: ErrorBannerProps) {
  if (errors.length === 0) return null

  const visible = errors.slice(0, maxDisplay)
  const hidden = errors.length - visible.length

  return (
    <div
      className="mb-3 space-y-1.5"
      role="alert"
      aria-live="polite"
      data-testid="error-banner"
    >
      {visible.map((err, idx) => (
        <ErrorItem
          key={err.id ?? idx}
          err={err}
          onDismiss={onDismiss ? () => onDismiss(err.id ?? idx) : undefined}
        />
      ))}
      {hidden > 0 && (
        <div className="px-3 py-1 text-[11px] text-muted-foreground">
          还有 {hidden} 个源失败(已隐藏以避免横幅占满页面)
        </div>
      )}
      {retryAll && errors.some(e => e.retry) && (
        <div className="flex items-center justify-end gap-2 text-[11px]">
          <button
            type="button"
            onClick={retryAll}
            className="flex items-center gap-1 rounded-md px-2 py-0.5 text-primary transition-colors hover:text-primary/80"
          >
            <RotateCw className="h-3 w-3" />
            全部重试
          </button>
        </div>
      )}
    </div>
  )
}

export default ErrorBanner

function ErrorItem({ err, onDismiss }: { err: SourceError; onDismiss?: () => void }) {
  const [hidden, setHidden] = useState(false)
  const dismissMs = err.auto_dismiss_ms ?? 0
  useEffect(() => {
    if (dismissMs > 0 && onDismiss) {
      const t = setTimeout(onDismiss, dismissMs)
      return () => clearTimeout(t)
    }
  }, [dismissMs, onDismiss])

  if (hidden) return null

  const shortMsg = (err.message || '服务不可用').slice(0, 80)

  return (
    <div className="flex items-center gap-2 rounded-lg border border-amber-300/40 bg-amber-50/60 dark:bg-amber-950/20 px-3 py-2 text-[12px]">
      <AlertTriangle className="h-3.5 w-3.5 text-amber-600 shrink-0" />
      <div className="min-w-0 flex-1">
        <span className="font-medium text-foreground/90">{err.source}</span>
        <span className="mx-1.5 text-muted-foreground">·</span>
        <span className="text-muted-foreground" title={err.message}>{shortMsg}</span>
      </div>
      {err.retry && (
        <button
          type="button"
          onClick={() => err.retry?.()}
          className="flex items-center gap-0.5 rounded-md px-2 py-0.5 text-[11px] text-primary transition-colors hover:text-primary/80"
          title="重试"
        >
          <RotateCw className="h-3 w-3" />
          重试
        </button>
      )}
      {onDismiss && (
        <button
          type="button"
          onClick={() => {
            setHidden(true)
            onDismiss()
          }}
          className="rounded-md p-0.5 text-muted-foreground transition-colors hover:text-foreground"
          title="忽略"
        >
          <X className="h-3 w-3" />
        </button>
      )}
    </div>
  )
}