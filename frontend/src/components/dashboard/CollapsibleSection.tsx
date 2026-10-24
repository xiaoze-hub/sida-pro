import { useCallback, useState, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'

import { readCollapsed, writeCollapsed } from '@/lib/dashboard-collapse'

/**
 * 首页证据层的一个可折叠块（2026-10-24 首屏重排）。
 *
 * 设计约束（都是踩过的）：
 *   · **表头永远在**：折叠不等于"这块没了"。表头必须自带 1–2 个关键数字，
 *     否则用户收起后还得再点开才能确认值 —— 那就不是折叠，是藏。
 *   · **收起时不挂 children**：重区块（面积图/列表）不该为看不见的东西跑取数与渲染。
 *   · **状态按块记**：`readCollapsed(id)` 走 localStorage，块与块互不影响。
 *   · 表头是**信息行**不是工具栏：标题 13/600、摘要 12/常规，只有右侧一个 28px 开关。
 */
export default function CollapsibleSection({
  id,
  header,
  children,
  className = '',
  bodyClassName = '',
  defaultCollapsed = true,
  onExpand,
}: {
  /** localStorage 键后缀（也是 data-testid 后缀），块内唯一即可 */
  id: string
  /** 表头内容（标题 + 关键数字），必须能独立读懂得出结论 */
  header: ReactNode
  children: ReactNode
  className?: string
  bodyClassName?: string
  defaultCollapsed?: boolean
  /** 首次展开时回调（用于"展开才取数"：首屏不该为看不见的块发请求） */
  onExpand?: () => void
}) {
  const [collapsed, setCollapsed] = useState(() => readCollapsed(id, defaultCollapsed))
  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev
      writeCollapsed(id, next)
      if (!next) onExpand?.()
      return next
    })
  }, [id, onExpand])

  return (
    <section
      data-testid={`evidence-${id}`}
      data-collapsed={collapsed ? 'true' : 'false'}
      className={className}
    >
      <div className="flex items-start justify-between gap-2 border-t border-border/60 pt-2.5">
        <div className="min-w-0 flex-1">{header}</div>
        <button
          type="button"
          data-testid={`evidence-toggle-${id}`}
          aria-expanded={!collapsed}
          onClick={toggle}
          className="flex min-h-[28px] shrink-0 items-center gap-0.5 rounded px-1.5 text-[11px] text-muted-foreground transition-colors hover:bg-s2 hover:text-foreground"
        >
          {collapsed ? '展开' : '收起'}
          <ChevronDown className={`h-3 w-3 transition-transform ${collapsed ? '' : 'rotate-180'}`} />
        </button>
      </div>
      {collapsed ? null : <div className={bodyClassName || 'mt-2'}>{children}</div>}
    </section>
  )
}
