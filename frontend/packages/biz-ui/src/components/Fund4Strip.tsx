/**
 * 四档资金结构条带图（2026-10-24 新增，数据源 `exday_daily` / 口径 `tdx_exday4`）。
 *
 * 为什么用自绘 SVG 而不是 lightweight-charts：
 *   四档是「同一天四个并列量」，lightweight-charts 的 histogram 只能一个 series 一组柱，
 *   四组挂同一价格轴会互相遮挡。这里一天画 4 根细柱，读的是「特大/大/中/小 谁在买谁在卖」，
 *   不需要坐标轴刻度，SVG 反而更准、更轻。
 *
 * 产品铁律遵循：
 *   - 不包卡片：外层只有 `border-t` hairline（去卡片化）
 *   - 买红卖绿：净额 >0 红、<0 绿（A 股惯例）
 *   - 不宣称方向：口径徽章是「数据出处」，值本身不做涨跌预测
 *   - 阈值标「推断值」：TQ 不返回档位阈值，界面上不能说成精确分档
 */
import { useMemo } from 'react'
import { cn } from '@panwatch/base-ui'
import CaliberBadge from './CaliberBadge'
// R6：项目已有「不用 toFixed」的金额格式化（负值也能分档），直接复用，不在这里再写一套
import { fmtSignedAmount } from '../lib/ladder-format'

export type Fund4Tier = { buy: number | null; sell: number | null; net: number | null }
export type Fund4Day = {
  trade_date: string
  amount?: number | null
  active_ratio?: number | null
  tiers: { xl: Fund4Tier; lg: Fund4Tier; md: Fund4Tier; sm: Fund4Tier }
}

const TIERS: Array<{ key: 'xl' | 'lg' | 'md' | 'sm'; label: string; hint: string }> = [
  { key: 'xl', label: '特', hint: '特大单（推断 >100万/笔）' },
  { key: 'lg', label: '大', hint: '大单（推断 20~100万/笔）' },
  { key: 'md', label: '中', hint: '中单（推断 5~20万/笔）' },
  { key: 'sm', label: '小', hint: '小单（推断 <5万/笔）' },
]

const DAY_W = 22 // 每天占宽(px)，含 4 根柱 + 间隔
const BAR_W = 4
const GAP = 1
const H = 92 // 柱区高度
const PAD_TOP = 4

function fmtAmount(v: number | null | undefined): string {
  return fmtSignedAmount(v)
}

/** 一位小数（R6：不用 toFixed，手动取整）。 */
function fixed1(v: number): string {
  return String(Math.round(v * 10) / 10)
}

export default function Fund4Strip({
  days,
  loading = false,
  className,
  hint,
}: {
  days: Fund4Day[]
  loading?: boolean
  className?: string
  /** 后端提示（表空/口径说明），如实显示，不吞掉 */
  hint?: string
}) {
  const { maxAbs, bars, zeroY } = useMemo(() => {
    let mx = 0
    for (const d of days) {
      for (const t of TIERS) {
        const v = d.tiers?.[t.key]?.net
        if (typeof v === 'number' && Number.isFinite(v)) mx = Math.max(mx, Math.abs(v))
      }
    }
    // 全 0 / 全空时不缩放，避免除 0
    const scale = mx > 0 ? mx : 1
    const zero = PAD_TOP + H / 2
    const out: Array<{ x: number; y: number; h: number; up: boolean; key: string; day: Fund4Day; tier: typeof TIERS[number] }> = []
    days.forEach((d, i) => {
      const gx = i * DAY_W
      TIERS.forEach((t, j) => {
        const v = d.tiers?.[t.key]?.net
        if (typeof v !== 'number' || !Number.isFinite(v) || v === 0) return
        const h = Math.max(1, (Math.abs(v) / scale) * (H / 2 - PAD_TOP))
        const up = v > 0
        out.push({
          x: gx + j * (BAR_W + GAP),
          y: up ? zero - h : zero,
          h,
          up,
          key: `${d.trade_date}-${t.key}`,
          day: d,
          tier: t,
        })
      })
    })
    return { maxAbs: mx, bars: out, zeroY: zero }
  }, [days])

  const w = Math.max(days.length * DAY_W, 120)

  return (
    <section className={cn('border-t border-border/60 pt-2', className)}>
      <header className="mb-1 flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <span className="text-[12px] text-foreground">四档资金结构</span>
        <CaliberBadge caliber="tdx_exday4" />
        <span className="opacity-70">主力净额 = 特大 + 大（买红卖绿）</span>
        {days.length > 0 && (
          <span className="ml-auto font-num tabular-nums opacity-60">
            {days[0].trade_date.slice(4)} ~ {days[days.length - 1].trade_date.slice(4)}
          </span>
        )}
      </header>

      {loading && <div className="py-6 text-center text-[11px] text-muted-foreground">四档数据加载中…</div>}

      {!loading && days.length === 0 && (
        <div className="py-5 text-[11px] text-muted-foreground">
          {hint || '暂无四档历史（该表自 2026-10-23 起每交易日 15:45 采集，历史不可回补）'}
        </div>
      )}

      {!loading && days.length > 0 && (
        <>
          <div className="flex items-stretch gap-1.5">
            {/* 档位图例：纵向对齐柱区 */}
            <div className="flex flex-col justify-between py-0 text-[10px] leading-none text-muted-foreground/70">
              {TIERS.map((t) => (
                <span key={t.key} title={t.hint}>
                  {t.label}
                </span>
              ))}
            </div>
            <div className="min-w-0 flex-1 overflow-x-auto scrollbar">
              <svg width={w} height={PAD_TOP * 2 + H} className="block">
                {/* 零轴 */}
                <line x1={0} y1={zeroY} x2={w} y2={zeroY} className="stroke-border" strokeWidth={0.5} />
                {bars.map((b) => (
                  <rect
                    key={b.key}
                    x={b.x}
                    y={b.y}
                    width={BAR_W}
                    height={b.h}
                    style={{ fill: b.up ? 'var(--stock-up)' : 'var(--stock-down)', opacity: 0.85 }}
                    // 用 inline style 而不是 Tailwind 任意值：`fill-[var(...)]` 依赖 JIT 扫到该类,
                    // 而 SVG 属性走 style 不经过构建期, 换主题/换打包器都不会静默失效
                    data-dir={b.up ? 'up' : 'down'}
                  >
                    <title>
                      {`${b.day.trade_date} ${b.tier.hint} 净额 ${fmtAmount(b.day.tiers[b.tier.key]?.net)}`}
                      {`\n买 ${fmtAmount(b.day.tiers[b.tier.key]?.buy)} / 卖 ${fmtAmount(b.day.tiers[b.tier.key]?.sell)}`}
                      {b.day.active_ratio != null ? `\n主动买占比 ${fixed1(b.day.active_ratio)}%` : ''}
                    </title>
                  </rect>
                ))}
              </svg>
            </div>
          </div>
          <div className="mt-1 flex items-center justify-between text-[10px] text-muted-foreground/60">
            <span title="档位阈值 TQ 未返回，界面上标注为推断值">
              档位阈值：特大&gt;100万 · 大20~100万 · 中5~20万 · 小&lt;5万（推断值）
            </span>
            <span className="font-num tabular-nums">峰值 |{fmtAmount(maxAbs)}|</span>
          </div>
        </>
      )}
    </section>
  )
}
