/**
 * 资金/数据口径徽章(2026-09-20 设计系统 P0: 口径统一为 10px 等宽徽章 + tooltip, 中性色)。
 *
 * 依据: 口径标签在 Dashboard/暗盘/chart 工具文本里形态各不相同; 而"这个数是哪个口径"恰恰是本
 * 项目最容易产生误读的地方(逐笔 vs 按单金额归类, 两个口径方向可能相反)。
 *
 * 铁律: **口径徽章不随涨跌上色** —— 它是"数据出处", 不是"行情方向"。
 */
import { cn } from '@panwatch/base-ui'

export type Caliber = 'tick' | 'eastmoney4' | 'tdx_exday4' | 'ths' | 'unknown'

const CALIBER_TEXT: Record<Caliber, { short: string; full: string }> = {
  tick: { short: '逐笔', full: '逐笔成交口径(腾讯 tick): 按主动买/主动卖方向统计, 可用于方向性判定' },
  eastmoney4: { short: '四档', full: '按单金额四档归类(东财): 超大/大/中/小单, 方向与逐笔口径可能不一致' },
  tdx_exday4: {
    short: '通达信四档',
    full: '通达信客户端按单笔成交金额分四档(tdx_exday4): 特大/大/中/小单。'
      + '档位阈值是推断值(TQ 未返回), 与东财四档(eastmoney4)、逐笔(tick) 不可换算',
  },
  ths: { short: '同花顺', full: '同花顺口径: 与逐笔口径的拆单识别不同, 不可与 tick 混用做方向判定' },
  unknown: { short: '口径未标', full: '该数值未携带口径标签, 按 unknown 处理 —— 不得用于方向性判定' },
}

export default function CaliberBadge({
  caliber,
  className,
  withText = true,
  label,
  title,
}: {
  caliber: Caliber
  className?: string
  withText?: boolean
  /** 后端只给了中文标签(没有原始口径值)时, 用 label 如实显示 —— 不假装成 unknown */
  label?: string
  /** 覆盖 tooltip(默认给该口径的完整说明) */
  title?: string
}) {
  const t = CALIBER_TEXT[caliber] ?? CALIBER_TEXT.unknown
  return (
    <span
      className={cn(
        'inline-flex items-center rounded border border-border px-1 font-mono text-[10px] leading-4 text-muted-foreground',
        className,
      )}
      data-caliber={caliber}
      title={title ?? t.full}
    >
      {label || (withText ? t.short : caliber === 'unknown' ? '?' : t.short.slice(0, 1))}
    </span>
  )
}

/** 后端返回的字段值 → 徽章口径(拿不到标签一律 unknown, 不猜)。 */
export function caliberOf(raw: string | null | undefined): Caliber {
  const v = (raw ?? '').trim().toLowerCase()
  if (v === 'tick' || v === 'eastmoney4' || v === 'ths') return v
  // 同花顺家族: 上游 source 常写具体通道名(`thsdk_dde` / `thsdk_alert` / `thsdk_tick` …),
  // 不是裸 `ths`。它们**都是同花顺口径**, 精确匹配会把它们全判成 unknown, 调用方随后容易
  // 「凑一个」eastmoney4 顶上 —— 这正是 DarkFundTop/WarReport 口径错标的根因。
  // 因此这里收敛前缀 → ths(只做口径归类, 不改任何数值, 四口径仍不可换算)。
  if (v.startsWith('thsdk') || v.startsWith('ths_')) return 'ths'
  // 通达信客户端四档(exday): 上游 source 写 `tdx_exday4` / `tdx_tq_exday`。
  // 与东财四档是**不同口径**(按单笔成交额 vs 按单金额归类), 不可归一成 eastmoney4。
  // 后端 `exday_structure.SOURCE` 实际写的是 `tdx_tq_exday`（不是 `tdx_exday4`），
  // 所以按「tdx_ 前缀 + 含 exday」判定，避免只认一种拼写。
  if (v === 'exday4' || (v.startsWith('tdx_') && v.includes('exday'))) return 'tdx_exday4'
  return 'unknown'
}
