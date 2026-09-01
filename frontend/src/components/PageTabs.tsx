import type { LucideIcon } from 'lucide-react'

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
}: {
  tabs: PageTabItem[]
  value: string
  onChange: (key: string) => void
  className?: string
}) {
  if (tabs.length === 0) return null
  return (
    <div
      className={`inline-flex max-w-full flex-wrap items-center gap-1 rounded-xl border border-border/50 bg-accent/40 p-1 ${className}`}
      role="tablist"
    >
      {tabs.map((t) => {
        const Icon = t.icon
        const active = t.key === value
        return (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={active}
            title={t.hint || t.label}
            onClick={() => onChange(t.key)}
            className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg px-2.5 py-1 text-[12px] font-medium transition-colors ${
              active
                ? 'bg-primary text-primary-foreground shadow-sm'
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
