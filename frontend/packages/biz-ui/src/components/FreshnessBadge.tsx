import { cn } from '@panwatch/base-ui'

/**
 * 新鲜度徽章(2026-09-20 设计系统 P0: 「新鲜度视觉语法」)。
 *
 * 由来: `stale` 此前散落在 15 个文件、`as_of` 6 处, 但全站**没有一个统一语法** ——
 * 同一个"这份数据是 10 分钟前的"在不同页面长得完全不同, 有的干脆不说。
 * 对行情终端来说这是最伤的一类问题: 用户看到数字却不知道它是不是活的。
 *
 * 语法(只有四态, 不发明第五种):
 *   绿 = live  (在容忍窗口内)
 *   琥珀 = delay (超出实时窗口但仍在可用窗口)
 *   灰 = stale (超窗 / **时间未知** —— 不知道时间就不能说它实时)
 *   灰 = closed (已收盘: 此时没有"实时"这回事, 不把静态数据标成 live)
 *
 * 配套: 页面根节点挂 `data-freshness="live|delay|stale|closed"`, 布局体检据此断言
 * "有 stale 数据就必须有可见角标"(规范变成机器判据, 否则没人跑)。
 */
export type Freshness = 'live' | 'delay' | 'stale' | 'closed'

export interface FreshnessInfo {
  level: Freshness
  /** 数据年龄(秒); 时间未知时为 null —— 不许瞎猜 0 */
  ageSec: number | null
  label: string
}

const LEVEL_LABEL: Record<Freshness, string> = {
  live: '实时',
  delay: '延迟',
  stale: '陈旧',
  closed: '已收盘',
}

/**
 * 由 `as_of` 时间戳判定新鲜度。
 * @param asOf 数据时间(ISO 串 / 毫秒 / Date); 缺失 → `stale`(时间未知不能说实时)
 * @param opts.maxLiveSec 实时窗口(默认 90s: 盘中 60s 轮询 + 抖动余量)
 * @param opts.maxDelaySec 可用窗口(默认 600s)
 * @param opts.marketClosed 收盘态(此时一律 closed, 与数据年龄无关)
 */
export function freshnessOf(
  asOf: string | number | Date | null | undefined,
  opts: { now?: number; maxLiveSec?: number; maxDelaySec?: number; marketClosed?: boolean } = {},
): FreshnessInfo {
  const { now = Date.now(), maxLiveSec = 90, maxDelaySec = 600, marketClosed = false } = opts
  if (marketClosed) return { level: 'closed', ageSec: null, label: LEVEL_LABEL.closed }
  const t = toMs(asOf)
  if (t === null) return { level: 'stale', ageSec: null, label: '时间未知' }
  const ageSec = Math.max(0, Math.round((now - t) / 1000))
  if (ageSec <= maxLiveSec) return { level: 'live', ageSec, label: LEVEL_LABEL.live }
  if (ageSec <= maxDelaySec) return { level: 'delay', ageSec, label: LEVEL_LABEL.delay }
  return { level: 'stale', ageSec, label: LEVEL_LABEL.stale }
}

function toMs(asOf: string | number | Date | null | undefined): number | null {
  if (asOf === null || asOf === undefined || asOf === '') return null
  if (asOf instanceof Date) return Number.isFinite(asOf.getTime()) ? asOf.getTime() : null
  if (typeof asOf === 'number') return Number.isFinite(asOf) ? asOf : null
  const t = Date.parse(asOf)
  return Number.isFinite(t) ? t : null
}

/** 年龄 → 「3s / 2m / 1h」; 未知返回 `--`(不是 0)。 */
export function formatAge(ageSec: number | null): string {
  if (ageSec === null) return '--'
  if (ageSec < 60) return `${ageSec}s`
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m`
  return `${Math.floor(ageSec / 3600)}h`
}

const LEVEL_CLASS: Record<Freshness, string> = {
  live: 'text-emerald-500 border-emerald-500/40',
  delay: 'text-amber-500 border-amber-500/40',
  stale: 'text-muted-foreground border-border',
  closed: 'text-muted-foreground border-border',
}

/**
 * 角标本体。**stale/closed 时时间戳一并淡化** —— 光有一个灰点不足以说明"这份数是旧的",
 * 让"数字本身"和"它的时间"在视觉上一起降权。
 */
export default function FreshnessBadge({
  info,
  asOf,
  className,
  showAge = true,
}: {
  info: FreshnessInfo
  /** 原始时间戳(展示用); 缺省不渲染时间 */
  asOf?: string | number | Date | null
  className?: string
  showAge?: boolean
}) {
  const stale = info.level === 'stale' || info.level === 'closed'
  return (
    <span
      className={cn('inline-flex items-center gap-1', className)}
      data-freshness-badge={info.level}
      title={
        info.ageSec === null
          ? `${info.label}(数据时间未知)`
          : `${info.label} · 数据时间距现在 ${formatAge(info.ageSec)}`
      }
    >
      <span className={cn('session-dot', LEVEL_CLASS[info.level].split(' ')[0])} />
      <span className={cn('text-[10px]', stale ? 'text-muted-foreground/70' : 'text-muted-foreground')}>
        {info.label}
      </span>
      {showAge && info.ageSec !== null && (
        <span className={cn('font-mono text-[10px]', stale ? 'text-muted-foreground/50' : 'text-muted-foreground/80')}>
          {formatAge(info.ageSec)}
        </span>
      )}
      {asOf !== undefined && asOf !== null && (
        <span className={cn('font-mono text-[10px]', stale ? 'text-muted-foreground/50' : 'text-muted-foreground/80')}>
          {formatClock(asOf)}
        </span>
      )}
    </span>
  )
}

/** 时间戳 → 「HH:MM:SS」(按浏览器本地时区; 取不到就返回空串, 不编时间)。 */
export function formatClock(asOf: string | number | Date | null | undefined): string {
  const t = toMs(asOf)
  if (t === null) return ''
  const d = new Date(t)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
