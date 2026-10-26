import { Shield } from 'lucide-react'
import { safeFixed, safeNum, safePercent } from '@/lib/format'
import { DEFENSE_LABEL, NO_DEFENSE_HINT, SECTION_DEFENSE } from '@/lib/copilot/copy'
import type { DefenseLine } from '@/lib/copilot/types'

/**
 * 关键防守位（支撑/压制两栏）。
 *
 * 铁律（设计稿 §4.9.2 / §8.5）：
 *  - **只渲染有数的**：`price` 不是有限数就整条丢掉，不留占位（缺 ≠ 0 价位）；
 *  - 一条都没有时明说「不给防守线（不猜价位）」，不拿现价凑一条；
 *  - 最多 4 条（后端已截断，前端再兜一层，卡片空间有限）。
 */
export function CopilotDefenseBlock({
  lines,
  close,
}: {
  lines: DefenseLine[]
  close: number | null
}) {
  const rows = (lines || [])
    .filter((l) => safeNum(l?.price) !== null)
    .slice(0, 4)
  const supports = rows.filter((l) => l.side === 'support')
  const resistances = rows.filter((l) => l.side === 'resistance')
  const closeTxt = safeFixed(close, 2)

  return (
    <section className="rounded-lg border border-border bg-s2 p-2" data-testid="copilot-defense">
      <div className="mb-1 flex items-center gap-1.5 text-[12px] font-medium text-muted-foreground">
        <Shield className="h-3 w-3" />
        {SECTION_DEFENSE}
        <span className="ml-auto font-mono text-[12px]">现价 {closeTxt}</span>
      </div>

      {rows.length === 0 ? (
        <div className="text-[12px] text-muted-foreground">{NO_DEFENSE_HINT}</div>
      ) : (
        <div className="space-y-1">
          {[...supports, ...resistances].map((l) => (
            <div
              key={`${l.slot}-${l.price}`}
              className="flex items-baseline gap-1.5 text-[12px]"
              data-testid={`copilot-defense-${l.slot}`}
            >
              <span
                className={`w-[54px] shrink-0 ${
                  l.side === 'support' ? 'text-role-opp' : 'text-role-risk'
                }`}
              >
                {DEFENSE_LABEL[l.slot] || l.label}
              </span>
              <span className="font-mono text-[12px] text-foreground">{safeFixed(l.price, 2)}</span>
              <span className="font-mono text-[12px] text-muted-foreground">
                {safePercent(l.distance_pct, 2, true)}
              </span>
              <span className="ml-auto truncate text-[12px] text-muted-foreground" title={l.basis}>
                {l.basis || (l.side === 'support' ? '支撑' : '压制')}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
