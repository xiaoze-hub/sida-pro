/**
 * 连板梯队晋级漏斗（2026-10-24，创新 1 补图）
 *
 * 为什么要有这张图：四列文字（首板 70 → 2板 9 → 3板 1 → 6板 2）能看"数"，看不出"断崖"。
 * 一眼能读懂的是**长度差**：70 掉到 9 那一刀有多深，比读 4 个数字快得多。
 *
 * 口径（与 REPTREE 规格一致，只画表内已有字段）：
 *  - 每层 `count` 是**表内真实家数**；`members` 是已落库明细（可能不全，与本图无关）。
 *  - **不画分层晋级率**：`promotion.per_level_available === false`（表内未存），
 *    本组件也不拿 count 相除现算——那会变成"看起来有据"的编造数字。
 *  - `count === 0` 画「无」而不是宽度 0 的空条（0 行与"没采集到"是两件事，前者如实）。
 *
 * 视觉：hairline 分隔、无卡片、无阴影；买红卖绿（涨停=红）；数字 tabular-nums。
 */
import type { ReplayTreeLevel } from '@panwatch/api'

export default function PromotionFunnel({
  levels,
  totalLimitUp,
}: {
  levels: ReplayTreeLevel[]
  /** 当日涨停总家数（画右侧「占涨停 N%」用；缺省则不显示占比列） */
  totalLimitUp?: number
}) {
  const asc = [...levels].sort((a, b) => a.board - b.board)
  if (!asc.length) {
    return (
      <div className="border-t border-border/60 py-3 text-[12px] text-muted-foreground">
        当日无梯队家数，无法绘制晋级漏斗。
      </div>
    )
  }
  const max = Math.max(...asc.map((l) => l.count), 1)
  return (
    <div className="border-t border-border/60 py-2" data-testid="promotion-funnel">
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <span className="text-[11px] text-muted-foreground">
          梯队家数（长度=家数，看断崖）
        </span>
        <span className="text-[10px] text-muted-foreground">
          首板→最高板共 {asc.length} 档 · 不含分层晋级率（表内未存）
        </span>
      </div>
      <div className="space-y-1" role="img" aria-label={`连板梯队家数: ${asc.map((l) => `${l.board === 1 ? '首板' : `${l.board}板`} ${l.count} 家`).join('，')}`}>
        {asc.map((lv) => {
          const label = lv.board === 1 ? '首板' : `${lv.board}板`
          const w = lv.count === 0 ? 0 : Math.max(2, (lv.count / max) * 100)
          const share =
            totalLimitUp && totalLimitUp > 0 && lv.count > 0
              ? Math.round((lv.count / totalLimitUp) * 100)
              : null
          return (
            <div key={lv.board} className="flex items-center gap-2" data-testid={`promotion-funnel-row-${lv.board}`}>
              <span className="w-9 shrink-0 text-right text-[11px] text-muted-foreground">{label}</span>
              <div className="relative h-3 min-w-0 flex-1 border-l border-border/40">
                {lv.count === 0 ? (
                  <span className="absolute inset-y-0 left-1 flex items-center text-[10px] text-muted-foreground">
                    无
                  </span>
                ) : (
                  <div
                    className="h-3"
                    style={{ width: `${w}%`, backgroundColor: 'var(--stock-up)', opacity: 0.8 }}
                    data-testid={`promotion-funnel-bar-${lv.board}`}
                  />
                )}
              </div>
              <span className="w-14 shrink-0 text-right font-num text-[12px] tabular-nums text-foreground">
                {lv.count === 0 ? '--' : `${lv.count} 家`}
              </span>
              <span className="w-16 shrink-0 text-right font-num text-[10px] tabular-nums text-muted-foreground">
                {share != null ? `占涨停 ${share}%` : ''}
              </span>
            </div>
          )
        })}
      </div>
      {max <= 1 && (
        <div className="mt-1 text-[10px] text-muted-foreground">
          各档家数均 ≤1，柱长已按最小值保留 2% 宽度以便可读（真实家数以右侧数字为准）。
        </div>
      )}
    </div>
  )
}
