import { X } from 'lucide-react'

import { chartContextParts, chartContextSummary, type ChartContext } from '@/lib/copilot/chart-context'

/**
 * 会话顶部的「图表上下文」条（P2, 2026-10-29）。
 *
 * 用户从个股工作台进参谋时，把屏幕上那张图的可证读数摊开写成一行：类型/市场、周期、
 * 可视区间、图上已确认价位。**只显示拿得到的**（`chartContextParts` 已按可证性过滤），
 * 末尾固定标「进入时快照」—— 防止用户把它当实时值，也防止模型把它当实时值。
 *
 * 为什么必须可见：这段文本会被真正塞进 `initial_context` 送给模型。界面上不展示，
 * 就成了"背地里给模型喂了用户没看见的内容"。清除按钮同理 —— 用户要能把它拿掉。
 */
export function ChartContextChip({ ctx, onClear }: { ctx: ChartContext; onClear: () => void }) {
  const parts = chartContextParts(ctx)
  return (
    <div
      data-testid="copilot-chart-context"
      title={chartContextSummary(ctx)}
      className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border bg-s2 px-3 py-1.5"
    >
      <span className="text-[12px] text-muted-foreground">图表上下文</span>
      <span className="font-mono text-[12px] text-role-watch">{ctx.symbol}</span>
      {parts.map((part) => (
        <span key={part} className="text-[12px] text-foreground">
          {part}
        </span>
      ))}
      <span className="text-[12px] text-muted-foreground">进入时快照</span>
      <button
        type="button"
        onClick={onClear}
        data-testid="copilot-chart-context-clear"
        title="不再把这个图表上下文带进提问"
        className="ml-auto flex items-center gap-0.5 text-[12px] text-muted-foreground hover:text-foreground"
      >
        <X className="h-3 w-3" aria-hidden="true" />
        清除
      </button>
    </div>
  )
}
