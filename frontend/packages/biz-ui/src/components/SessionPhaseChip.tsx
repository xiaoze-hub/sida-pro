import { cn } from '@panwatch/base-ui'

/**
 * 会话态 chip(2026-09-20 设计系统 P0)。
 *
 * 依据: 会话态此前只存在于 `/theme-mood` 内部的 `MarketPhasePanel`, 全局顶栏没有任何"现在是什么
 * 时段"的指示 —— 于是用户看到 9:20 的数和 14:30 的数长得一模一样。
 *
 * 约束(用户定的铁律): **会话态只允许顶栏极轻指示, 禁止整页染色**。
 * 这里只输出一枚 muted 文字 + 6px 圆点; 背景色一律不参与(背景只表达空间与会话的"位置", 不染色)。
 */
export type SessionPhase = 'pre' | 'auction' | 'morning' | 'noon' | 'afternoon' | 'closed' | 'weekend'

export interface SessionInfo {
  phase: SessionPhase
  label: string
  /** 该时段是否处于"报价连续跳动"的状态(顶栏据此决定要不要显示实时徽章) */
  ticking: boolean
}

const PHASE_LABEL: Record<SessionPhase, string> = {
  pre: '盘前',
  auction: '集合竞价',
  morning: '连续竞价',
  noon: '午休',
  afternoon: '连续竞价',
  closed: '已收盘',
  weekend: '休市',
}

/**
 * 按**北京时间**判定会话态(不依赖浏览器时区)。
 * A 股无夏令时 ⇒ 直接 UTC+8 算术最稳, 不受用户机器时区/夏令时影响。
 * 时段: 09:15–09:25 集合竞价 / 09:25–09:30 静默(归盘前) / 09:30–11:30 上午
 *       11:30–13:00 午休 / 13:00–15:00 下午 / 其余为盘前/已收盘; 周末休市。
 */
export function sessionPhaseOf(when: Date = new Date()): SessionInfo {
  const cn = new Date(when.getTime() + 8 * 3600 * 1000)
  const day = cn.getUTCDay()
  if (day === 0 || day === 6) return { phase: 'weekend', label: PHASE_LABEL.weekend, ticking: false }
  const mins = cn.getUTCHours() * 60 + cn.getUTCMinutes()
  const at = (h: number, m: number) => h * 60 + m
  if (mins < at(9, 15)) return { phase: 'pre', label: PHASE_LABEL.pre, ticking: false }
  if (mins < at(9, 25)) return { phase: 'auction', label: PHASE_LABEL.auction, ticking: true }
  if (mins < at(9, 30)) return { phase: 'pre', label: PHASE_LABEL.pre, ticking: false }
  if (mins < at(11, 30)) return { phase: 'morning', label: PHASE_LABEL.morning, ticking: true }
  if (mins < at(13, 0)) return { phase: 'noon', label: PHASE_LABEL.noon, ticking: false }
  if (mins < at(15, 0)) return { phase: 'afternoon', label: PHASE_LABEL.afternoon, ticking: true }
  return { phase: 'closed', label: PHASE_LABEL.closed, ticking: false }
}

/** 顶栏会话态指示: 极轻, 不抢任何行情色。 */
export default function SessionPhaseChip({ info, className }: { info: SessionInfo; className?: string }) {
  return (
    <span
      className={cn('inline-flex items-center gap-1.5 text-[11px] text-muted-foreground', className)}
      data-session-phase={info.phase}
      title={`当前时段: ${info.label}(按北京时间判定)`}
    >
      <span className={cn('session-dot', info.ticking ? 'text-emerald-500' : 'text-muted-foreground/60')} />
      {info.label}
    </span>
  )
}
