import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * 术语解释（2026-10-24, L2-F / 审计 G11）
 *
 * 背景：首页/决策/机会等页面把 `封板率`、`晋级`、`HHI`、`SUPAMO`、`EWMA`、`p72`、`IC/IR`
 * 直接铺在界面上，新用户无法自解。本组件是**唯一的**术语解释出口，禁止各页自造第二套。
 *
 * 设计约束：
 * - 不用 portal：弹层是工作台「多标签页保活」下最容易被带走的元素（同 Lane B 的坑），
 *   这里用相对定位气泡，页面切走即随组件消失。
 * - 键盘可达：`Tab` 可聚焦 `?`，`Enter/Space` 切换，`Esc` 关闭，`aria-expanded` 同步。
 * - 术语文案必须来自口径源（`src/core/caliber.py` / `specs/api/*.md`），**不许凭空写解释**（§8 硬约束⑩）。
 */
export interface TermProps {
  /** 界面上显示的原词，例如 `封板率` */
  children: ReactNode
  /** 1 句人话解释；必须是可复核的口径描述，不是营销文案 */
  def: ReactNode
  /** 可选：跳到口径/指标文档 */
  href?: string
  /** 气泡朝向，默认下方 */
  side?: 'top' | 'bottom'
  className?: string
}

export function Term({ children, def, href, side = 'bottom', className }: TermProps) {
  const [open, setOpen] = useState(false)
  const tipId = useId()
  const wrapRef = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    const onPointerDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onPointerDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onPointerDown)
    }
  }, [open])

  return (
    <span
      ref={wrapRef}
      className={cn('relative inline-flex items-center gap-0.5 align-baseline', className)}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <span className="cursor-help border-b border-dashed border-muted-foreground/50">{children}</span>
      <button
        type="button"
        aria-label="术语说明"
        aria-expanded={open}
        aria-describedby={open ? tipId : undefined}
        onClick={(e) => {
          e.stopPropagation()
          setOpen((v) => !v)
        }}
        onFocus={() => setOpen(true)}
        onBlur={(e) => {
          if (!wrapRef.current?.contains(e.relatedTarget as Node)) setOpen(false)
        }}
        className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border border-border/70 text-[12px] leading-none text-muted-foreground hover:border-primary/50 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
      >
        ?
      </button>
      {open && (
        <span
          role="tooltip"
          id={tipId}
          className={cn(
            'absolute left-0 z-50 w-60 rounded-lg border border-border bg-s4 p-2 text-[12px] leading-relaxed text-foreground shadow-float',
            side === 'top' ? 'bottom-full mb-1' : 'top-full mt-1',
          )}
        >
          {def}
          {href && (
            <a
              href={href}
              className="mt-1 block text-[12px] text-primary hover:underline"
              onClick={(e) => e.stopPropagation()}
            >
              查看口径文档
            </a>
          )}
        </span>
      )}
    </span>
  )
}

export default Term
