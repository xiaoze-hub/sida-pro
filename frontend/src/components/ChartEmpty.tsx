import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

/**
 * 图表空态（2026-09-18 UI 走查 B2）。
 *
 * 走查发现 5 个页面在**无数据**时仍然把图表容器撑满（`heatmap` 空态占 560px、
 * `paper-trading` 空曲线占 192px…）→ 半屏甚至整屏空白，信息传达失效（视觉分被拉到 5/10）。
 *
 * 约定：
 * - 无数据一律**压缩**到 `height`（默认 132px）并写清"为什么没有"；
 * - 只说"没有"，**绝不显示 0**（把 0 当无数据是禁的）；
 * - 给一个可点的下一步（action），而不是让用户对着空白发呆。
 */
export interface ChartEmptyProps {
  /** 主文案：为什么这里是空的（默认「暂无数据」） */
  title?: string
  /** 补充说明：数据什么时候来 / 怎么才会有 */
  description?: string
  /** 建议高度（px）。默认 132 —— 紧凑，但能容纳一行标题 + 一行说明 + 一个按钮 */
  height?: number
  /** 可选下一步（按钮/链接） */
  action?: ReactNode
  className?: string
}

export function ChartEmpty({
  title = '暂无数据',
  description,
  height = 132,
  action,
  className,
}: ChartEmptyProps) {
  return (
    <div
      data-chart-empty="1"
      role="status"
      style={{ minHeight: `${height}px` }}
      className={cn(
        'flex flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-border/60 bg-muted/10 px-4 py-4 text-center',
        className,
      )}
    >
      <p className="text-[12px] text-muted-foreground">{title}</p>
      {description && (
        <p className="max-w-md text-[11px] leading-relaxed text-muted-foreground/80">{description}</p>
      )}
      {action && <div className="mt-1.5">{action}</div>}
    </div>
  )
}

export default ChartEmpty
