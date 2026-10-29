import type { ReadinessItem } from '@/lib/copilot/evidence-readiness'
import { readinessProgressLabel, statusText } from '@/lib/copilot/evidence-readiness'

/**
 * 证据齐套度条（2026-10-23，参谋 UX）。
 *
 * 一条**单行**痕迹：`已取 3/10 · 行情 ✓ · 技术面 ✓ · 逐笔 ⏳ · 撤单 未取到`。
 * 流中实时更新，回答定稿后仍留在回答顶部 —— 用户能回看「这份结论到底取到了哪几路证据」。
 *
 * 版式（行情终端口径，不是卡片仪表盘）：
 *  - 没有新卡片：上下各一条 hairline（border-border）+ 行距分隔；
 *  - 没有阴影（popover 之外禁 elevation）、没有彩色背景；
 *  - 字号 12px（辅助文字下限，见设计稿勘误 7.1：10px 已禁用），数字 tabular-nums 防跳动；
 *  - 颜色只带语义：失败用 role-risk，其余一律 muted —— 成功不是「喜庆绿」。
 *
 * 空态：`items` 为空直接不渲染（没取数就不该出现一条空条，更不该显示 0/0）。
 * 分母未知（stage 标签没有 `/N`）时只显示「已取 N 项」，绝不编一个总数。
 *
 * 2026-10-29：`unknown`（未确认）单独计数。此前只有 `未取到 N` —— 流结束时既没成功证据
 * 也没失败证据的条目只挂一个「未确认」pill，条数不进任何计数，用户会把它当成「还没跑完」
 * 甚至默认它成功了。unknown 既不是 ok 也不是 failed，必须自己说出来。
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
  /** 未确认 = 本轮结束仍没有成功/失败证据的条目。既不算已取，也不算未取到。 */
  const unknownCount = items.filter((it) => it.status === 'unknown').length
  const progress = readinessProgressLabel({ done, total })

  return (
    <div
      data-testid="evidence-readiness"
      /*
       * 来源（2026-10-29 P1）：这些 pill 全部折自**本条回答自己的流式 `stage` 事件**
       * （`evidence-readiness.ts` 文件头的事件契约），所以这一层是已核验的 ——
       * 但它说明的是「本轮取了哪几路」，不是「哪一句依据来自哪一路」。
       * 两层不许混，逐句出处见 `EvidenceTraceNote`。
       */
      data-provenance="stream-stage"
      aria-label={`证据齐套度 ${progress}`}
      className="flex flex-wrap items-center gap-x-1.5 gap-y-1 border-y border-border py-1"
    >
      <span
        data-testid="evidence-readiness-progress"
        title="统计自本条回答的取数流（已核验）；它说明本轮取了哪几路，不含任何逐句依据的出处"
        className="shrink-0 text-[12px] tabular-nums text-muted-foreground"
      >
        {progress}
      </span>
      {failedCount > 0 ? (
        <span
          data-testid="evidence-readiness-failed-count"
          className="shrink-0 text-[12px] tabular-nums text-role-risk"
        >
          未取到 {failedCount}
        </span>
      ) : null}
      {unknownCount > 0 ? (
        <span
          data-testid="evidence-readiness-unknown-count"
          title="流结束时仍没有成功或失败证据：既不等于取到，也不等于取不到"
          className="shrink-0 text-[12px] tabular-nums text-amber-500"
        >
          未确认 {unknownCount}
        </span>
      ) : null}
      <span aria-hidden className="shrink-0 text-[12px] text-muted-foreground">
        ·
      </span>
      {items.map((it) => {
        const text = statusText(it.status)
        const tone =
          it.status === 'failed'
            ? 'border-role-risk/40 text-role-risk'
            : it.status === 'ok'
              ? 'border-border text-muted-foreground'
              : 'border-border text-foreground'
        return (
          <span
            key={it.key}
            data-testid="evidence-readiness-item"
            data-key={it.key}
            data-status={it.status}
            data-group={it.group}
            title={`${it.label}（${it.group}）`}
            className={`inline-flex shrink-0 items-baseline gap-1 rounded border px-1 py-[1px] text-[12px] tabular-nums ${tone}`}
          >
            <span>{it.label}</span>
            <span className="text-[12px]">{text}</span>
          </span>
        )
      })}
    </div>
  )
}

export default EvidenceReadinessStrip
