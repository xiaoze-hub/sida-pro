/**
 * 资金/数据口径徽章(2026-09-20 设计系统 P0: 口径统一为 10px 等宽徽章 + tooltip, 中性色)。
 *
 * 依据: 口径标签在 Dashboard/暗盘/chart 工具文本里形态各不相同; 而"这个数是哪个口径"恰恰是本
 * 项目最容易产生误读的地方(逐笔 vs 按单金额归类, 两个口径方向可能相反)。
 *
 * 铁律: **口径徽章不随涨跌上色** —— 它是"数据出处", 不是"行情方向"。
 */
import { cn } from '@panwatch/base-ui'

export type Caliber = 'tick' | 'eastmoney4' | 'ths' | 'unknown'

const CALIBER_TEXT: Record<Caliber, { short: string; full: string }> = {
  tick: { short: '逐笔', full: '逐笔成交口径(腾讯 tick): 按主动买/主动卖方向统计, 可用于方向性判定' },
  eastmoney4: { short: '四档', full: '按单金额四档归类(东财): 超大/大/中/小单, 方向与逐笔口径可能不一致' },
  ths: { short: '同花顺', full: '同花顺口径: 与逐笔口径的拆单识别不同, 不可与 tick 混用做方向判定' },
  unknown: { short: '口径未标', full: '该数值未携带口径标签, 按 unknown 处理 —— 不得用于方向性判定' },
}

export default function CaliberBadge({
  caliber,
  className,
  withText = true,
}: {
  caliber: Caliber
  className?: string
  withText?: boolean
}) {
  const t = CALIBER_TEXT[caliber] ?? CALIBER_TEXT.unknown
  return (
    <span
      className={cn(
        'inline-flex items-center rounded border border-border px-1 font-mono text-[10px] leading-4 text-muted-foreground',
        className,
      )}
      data-caliber={caliber}
      title={t.full}
    >
      {withText ? t.short : caliber === 'unknown' ? '?' : t.short.slice(0, 1)}
    </span>
  )
}

/** 后端返回的字段值 → 徽章口径(拿不到标签一律 unknown, 不猜)。 */
export function caliberOf(raw: string | null | undefined): Caliber {
  const v = (raw ?? '').toLowerCase()
  if (v === 'tick' || v === 'eastmoney4' || v === 'ths') return v
  return 'unknown'
}
