import type { ReadinessItem } from '@/lib/copilot/evidence-readiness'
import { readinessProgressLabel, statusText } from '@/lib/copilot/evidence-readiness'

/**
 * 证据齐套度条（2026-10-23，参谋 UX）。
 *
 * 一条**单行**痕迹：`已取 3/10 · 行情 ✓ · 技术面 ✓ · 逐笔 ⏳ · 撤单 未取到`。
 * 流中实时更新，回答定稿后仍留在回答顶部 —— 用户能回看「这份结论到底取到了哪几路证据」。
 *
 * 版式（行情终端口径，不是卡片仪表盘）：
 *  - 没有新卡片：上下各一条 hairline（border-border/40）+ 行距分隔；
 *  - 没有阴影（popover 之外禁 elevation）、没有彩色背景；
 *  - 字号只有 10px；数字 tabular-nums 防跳动；
 *  - 颜色只带语义：失败用 role-risk，其余一律 muted —— 成功不是「喜庆绿」。
 *
 * 空态：`items` 为空直接不渲染（没取数就不该出现一条空条，更不该显示 0/0）。
 * 分母未知（stage 标签没有 `/N`）时只显示「已取 N 项」，绝不编一个总数。
 */
export function EvidenceReadinessStrip({
  items,
  done,
  total,
  failed = 0,
}: {
  items: ReadinessItem[]
  done: number
  total: number | null
  /** 未取到的条数（可选；缺省按 items 里的 failed 统计） */
  failed?: number
}) {
  if (!items.length) return null

  const failedCount = failed || items.filter((it) => it.status === 'failed').length
  const progress = readinessProgressLabel({ done, total })

  return (
    <div
      data-testid="evidence-readiness"
      aria-label={`证据齐套度 ${progress}`}
      className="flex flex-wrap items-center gap-x-1.5 gap-y-1 border-y border-border/40 py-1"
    >
      <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">{progress}</span>
      {failedCount > 0 ? (
        <span
          data-testid="evidence-readiness-failed-count"
          className="shrink-0 text-[10px] tabular-nums text-role-risk"
        >
          未取到 {failedCount}
        </span>
      ) : null}
      <span aria-hidden className="shrink-0 text-[10px] text-muted-foreground/40">
        ·
      </span>
      {items.map((it) => {
        const text = statusText(it.status)
        const tone =
          it.status === 'failed'
            ? 'border-role-risk/40 text-role-risk'
            : it.status === 'ok'
              ? 'border-border/40 text-muted-foreground'
              : 'border-border/60 text-foreground/80'
        return (
          <span
            key={it.key}
            data-testid="evidence-readiness-item"
            data-key={it.key}
            data-status={it.status}
            data-group={it.group}
            title={`${it.label}（${it.group}）`}
            className={`inline-flex shrink-0 items-baseline gap-1 rounded border px-1 py-[1px] text-[10px] tabular-nums ${tone}`}
          >
            <span>{it.label}</span>
            <span className="text-[10px]">{text}</span>
          </span>
        )
      })}
    </div>
  )
}

export default EvidenceReadinessStrip
