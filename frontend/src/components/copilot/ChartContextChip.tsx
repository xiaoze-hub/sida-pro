import { X } from 'lucide-react'

import { chartContextParts, chartContextSummary, type ChartContext } from '@/lib/copilot/chart-context'

/**
 * 会话顶部的「图表上下文」条（P2, 2026-10-29）。
 *
 * 用户从个股工作台进参谋时，把屏幕上那张图的可证读数摊开写成一行：类型/市场、周期、
 * 可视区间、图上已确认价位。**只显示拿得到的**（`chartContextParts` 已按可证性过滤）。
 *
 * 状态必须如实（2026-10-29 修正）：这段文本一旦随 `initial_context` 建了会话，就落库在
 * `chat_conversations.initial_context` 上、后端每轮都会重新拼进系统提示，**没有接口能撤回**。
 * 所以：
 *  - 还没发问：明写「尚未送出」，清除就是本地丢弃；
 *  - 已送进本会话：明写「已送入本会话 · 不可撤回」，清除按钮变成「清除并新开会话」
 *    （只有换一个不带快照的会话，后端才会真的不再用旧读数），正在回答时禁用避免打断。
 *
 * 末尾固定标「进入时快照 · 非实时」—— 防止用户把它当实时值，也防止模型把它当实时值。
 */
export function ChartContextChip({
  ctx,
  committed,
  expired,
  busy,
  onClear,
}: {
  ctx: ChartContext
  /** 这段快照是否已经随本会话送达后端（会话级 initial_context，撤不回来） */
  committed?: boolean
  /** 超过 TTL：只作为"曾经看到过什么"展示，不会发送 */
  expired?: boolean
  /** 正在流式回答：清除会打断本轮，按钮先禁用 */
  busy?: boolean
  onClear: () => void
}) {
  const parts = chartContextParts(ctx)
  const state = expired
    ? '已过期（超过 5 分钟）· 不会发送'
    : committed
      ? '已送入本会话 · 不可撤回'
      : '尚未送出'
  return (
    <div
      data-testid="copilot-chart-context"
      data-committed={committed ? 'true' : 'false'}
      data-expired={expired ? 'true' : 'false'}
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
      <span className="text-[12px] text-muted-foreground">{state}</span>
      <span className="text-[12px] text-muted-foreground">进入时快照 · 非实时</span>
      <button
        type="button"
        onClick={onClear}
        disabled={busy}
        data-testid="copilot-chart-context-clear"
        title={
          busy
            ? '本轮还在回答，结束后再清除（不会打断当前回答）'
            : committed
              ? '本会话已带入该快照且服务端撤不回：点此新开一个不带它的会话（上一会话保留）'
              : '不再把这个图表上下文带进提问'
        }
        className="ml-auto flex items-center gap-0.5 text-[12px] text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
      >
        <X className="h-3 w-3" aria-hidden="true" />
        {committed ? '清除并新开会话' : '清除'}
      </button>
    </div>
  )
}
