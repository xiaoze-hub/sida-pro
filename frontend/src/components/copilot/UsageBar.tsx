import { safeFixed, safeInt, safeNum } from '@/lib/format'

/**
 * 用量条（今日 tokens / 成本 / 调用次数 / 配额进度）。
 *
 * 空数据语义（**为什么是 `--` 不是 `0`**）：
 *   `null` 表示"后端没给这一项"（无记录 / 未计费 / 接口失败），不是"这一项是 0"。
 *   写成 0 会把"没数据"渲染成"今天没用过"，用户会以为用度是 0 而忽略配额告警 ——
 *   与全站口径一致（`@/lib/format` 的 safe* 系列 + R7 门禁「禁 null→0 三元」）：
 *   缺数据一律 `--`，**不补 0**。
 *
 * 布局：一条窄行 + 配额细线（不是进度条卡片）。超 80% 转橙、超 100% 转红。
 */

export interface UsageBarProps {
  /** 今日 token 合计；null = 没记录 */
  tokens?: number | null
  /** 今日估算费用（元）；null = 未定价 / 算不出来 */
  cost?: number | null
  /** 今日调用次数；null = 没记录 */
  calls?: number | null
  /** 配额上限；null / undefined = 不限（没有配额行） */
  quota?: number | null
  /** 已用量（用于算配额百分比）；null = 没记录 */
  used?: number | null
  /** 标签前缀（默认"今日"） */
  label?: string
  className?: string
}

/** 配额占比：≥100% 红 / ≥80% 橙 / 其余中性。拿不到数就不给百分比（不编造）。 */
export function quotaLevel(ratio: number | null): 'ok' | 'warn' | 'over' {
  if (ratio === null) return 'ok'
  if (ratio >= 100) return 'over'
  if (ratio >= 80) return 'warn'
  return 'ok'
}

const LEVEL_CLS: Record<'ok' | 'warn' | 'over', string> = {
  ok: 'text-muted-foreground',
  warn: 'text-amber-500',
  over: 'text-red-500',
}

const BAR_CLS: Record<'ok' | 'warn' | 'over', string> = {
  ok: 'bg-muted-foreground/50',
  warn: 'bg-amber-500',
  over: 'bg-red-500',
}

/**
 * 进度条宽度（百分比，0~100）。
 * 拿不到比例 → 0%（空条，不画一段假进度）。这里返回 0 是**像素宽度**，
 * 不是展示给用户的读数；用户看到的百分比在 ratio 为 null 时是 `--`。
 */
function barWidth(ratio: number | null): number {
  if (ratio === null) return 0
  return Math.min(100, Math.max(0, ratio))
}

export function UsageBar({
  tokens,
  cost,
  calls,
  quota,
  used,
  label = '今日',
  className,
}: UsageBarProps) {
  const quotaNum = safeNum(quota)
  const usedNum = safeNum(used)
  // 配额百分比：分母缺失（不限）或分子缺失（没记录）→ null，不写 0%
  const ratio =
    quotaNum !== null && quotaNum > 0 && usedNum !== null ? (usedNum / quotaNum) * 100 : null
  const level = quotaLevel(ratio)
  const pctWidth = barWidth(ratio)

  const costText = safeNum(cost) === null ? '--' : `¥${safeFixed(cost, 2)}`

  return (
    <div
      className={`flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-muted-foreground ${className ?? ''}`}
      data-testid="copilot-usage-bar"
    >
      <span className="flex items-baseline gap-1">
        <span>{label} tokens</span>
        <span className="font-mono text-foreground" data-testid="copilot-usage-tokens">
          {safeInt(tokens)}
        </span>
      </span>

      <span className="flex items-baseline gap-1">
        <span>{label}成本</span>
        <span className="font-mono text-foreground" data-testid="copilot-usage-cost">
          {costText}
        </span>
      </span>

      <span className="flex items-baseline gap-1">
        <span>调用次数</span>
        <span className="font-mono text-foreground" data-testid="copilot-usage-calls">
          {safeInt(calls)}
        </span>
      </span>

      <span className="flex items-center gap-1" data-testid="copilot-usage-quota">
        <span>配额</span>
        {/* 百分比读数与上限读数分开占位：上限有值而用量没记录时，
            百分比是 `--`（缺数据），上限另起一格显示，不混在一格里换算。 */}
        <span
          className={`font-mono ${ratio === null ? 'text-muted-foreground' : LEVEL_CLS[level]}`}
          data-testid="copilot-usage-quota-text"
          data-level={level}
        >
          {ratio === null ? '--' : `${safeFixed(ratio, 0)}%`}
        </span>
        {quotaNum === null ? null : (
          <span
            className="font-mono text-muted-foreground/70"
            data-testid="copilot-usage-quota-limit"
          >
            / {safeInt(quotaNum)}
          </span>
        )}
        {/* 进度用细线（1px），不是进度条卡片 */}
        <span className="relative inline-block h-px w-[52px] bg-border/60" aria-hidden>
          <span
            className={`absolute left-0 top-0 h-px ${BAR_CLS[level]}`}
            style={{ width: `${pctWidth}%` }}
            data-testid="copilot-usage-quota-bar"
            data-level={level}
          />
        </span>
      </span>
    </div>
  )
}

export default UsageBar
