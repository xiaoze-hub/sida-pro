import type { LucideIcon } from 'lucide-react'
import { useId, type KeyboardEvent } from 'react'

/**
 * 受控页面内 Tab 栏(设计稿 §4.3 Tab 合并通用件)。
 *
 * 样式沿用页面内既有分段控件(圆角容器 + 内部按钮, 与 K 线周期切换器一致),
 * 不引入新视觉语言; Tab 状态由父组件持有(通常写进 URL 的 ?tab=, 见 TabbedPage)。
 */
export interface PageTabItem {
  key: string
  label: string
  icon?: LucideIcon
  /** 权限点: 不传 = 不做权限过滤 */
  perm?: string
  /** 仅 owner 可见(审计等敏感页) */
  ownerOnly?: boolean
  /** 折叠/收起态下 tooltip */
  hint?: string
}

export default function PageTabs({
  tabs,
  value,
  onChange,
  className = '',
  idPrefix,
  panelId,
}: {
  tabs: PageTabItem[]
  value: string
  onChange: (key: string) => void
  className?: string
  idPrefix?: string
  panelId?: string
}) {
  const generatedId = useId()
  const prefix = idPrefix ?? generatedId
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = tabs.findIndex((tab) => tab.key === value)
    let next = index
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
    else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    else return
    event.preventDefault()
    onChange(tabs[next].key)
    event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
  }
  if (tabs.length === 0) return null
  return (
    <div
      className={`inline-flex max-w-full flex-wrap items-center gap-1 rounded-lg border border-border bg-s2 p-1 ${className}`}
      role="tablist"
      aria-label="页面视图"
      onKeyDown={onKeyDown}
    >
      {tabs.map((t) => {
        const Icon = t.icon
        const active = t.key === value
        return (
          <button
            key={t.key}
            type="button"
            role="tab"
            id={`${prefix}-${t.key}`}
            aria-selected={active}
            aria-controls={panelId}
            tabIndex={active ? 0 : -1}
            title={t.hint || t.label}
            onClick={() => onChange(t.key)}
            className={`inline-flex min-h-9 items-center gap-1.5 whitespace-nowrap rounded-md px-3 py-1.5 text-[14px] font-medium transition-colors ${
              active
                ? 'bg-card text-foreground shadow-sm ring-1 ring-border'
                : 'text-muted-foreground hover:bg-accent hover:text-foreground'
            }`}
          >
            {Icon && <Icon className="h-3.5 w-3.5 shrink-0" />}
            <span>{t.label}</span>
          </button>
        )
      })}
    </div>
  )
}
