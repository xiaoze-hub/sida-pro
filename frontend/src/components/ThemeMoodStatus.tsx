/**
 * 题材情绪页「数据状态」徽标(2026-10-08)。
 *
 * 契约(后端另一路同步实现): `GET /api/theme-mood/board` 顶层新增
 *   phase: 'pre' | 'live' | 'closed_pending' | 'final'
 *   as_of / settled_at(ISO8601, +08:00) / trading_day / note
 *
 * 后端字段可能**先到或后到** → 这里做**容错解析**: 只认四个契约值, 其余(含字段缺失、
 * 旧后端、未知枚举)一律渲染成显式的「未知状态」, **绝不假装**成 live/final。
 *
 * 铁律: 本组件只输出一行内联徽标, 不引卡片/网格/图表; 布局与密度由 ThemeMood.tsx 现有
 * 结构承载(theme-mood-layering / density 契约测试是钉子)。字号只用 10/11px(字阶棘轮)。
 */
import type { ReactNode } from 'react'

/** 契约枚举(后端可能的值)。 */
export const BOARD_PHASES = ['pre', 'live', 'closed_pending', 'final'] as const
export type KnownBoardPhase = (typeof BOARD_PHASES)[number]
/** 未知态(字段缺失 / 值不在枚举内)是**显式**的一档, 不是兜底默认。 */
export type BoardPhase = KnownBoardPhase | 'unknown'

/** 板级实时状态相关字段(全部可缺省 → 容错)。 */
export interface BoardPhaseFields {
  phase?: string | null
  as_of?: string | null
  settled_at?: string | null
  trading_day?: boolean | null
  note?: string | null
}

/**
 * 容错解析 phase: 只认四个契约值; 缺省/非字符串/枚举外 → 'unknown'。
 * 注意: 不是 `?? 'pre'` 之类的"默认值" —— 那是猜; 未知就是未知。
 */
export function parseBoardPhase(raw: BoardPhaseFields | null | undefined): BoardPhase {
  const p = raw?.phase
  if (typeof p !== 'string') return 'unknown'
  return (BOARD_PHASES as readonly string[]).includes(p) ? (p as KnownBoardPhase) : 'unknown'
}

const PHASE_LABEL: Record<BoardPhase, string> = {
  pre: '盘前',
  live: '实时',
  closed_pending: '收盘待定型',
  final: '已定型',
  unknown: '未知状态',
}

/** 徽标底色只用 token 类(不许新增 bg-accent/NN、bg-stock-* 背景 —— surface 棘轮)。 */
const PHASE_CLASS: Record<BoardPhase, string> = {
  pre: 'bg-muted/50 text-muted-foreground',
  live: 'bg-primary/15 text-primary',
  closed_pending: 'bg-primary/10 text-foreground/70',
  final: 'bg-primary/15 text-primary',
  unknown: 'bg-muted/50 text-muted-foreground',
}

/** 快照/定型时间显式标注: 空值输出「无数据」(铁律: 缺数据不填 0、不假装)。 */
export function fmtSnapTime(iso: string | null | undefined): string {
  if (typeof iso !== 'string' || iso.length === 0) return '无数据'
  return iso
}

/** 各状态的数据时间文案(只有相关状态才给时间, 其余不硬凑)。 */
export function phaseTimeText(phase: BoardPhase, fields: BoardPhaseFields): string | null {
  if (phase === 'final') return `定型 ${fmtSnapTime(fields.settled_at)}`
  if (phase === 'live' || phase === 'closed_pending') return `快照 ${fmtSnapTime(fields.as_of)}`
  return null
}

export function ThemeMoodStatus(props: {
  phase: BoardPhase
  as_of?: string | null
  settled_at?: string | null
  trading_day?: boolean | null
  note?: string | null
}): ReactNode {
  const { phase, as_of, settled_at, trading_day, note } = props
  const time = phaseTimeText(phase, { as_of, settled_at })
  return (
    <span
      data-testid="thememood-phase-badge"
      data-phase={phase}
      title={note || undefined}
      className="flex items-center gap-1.5"
    >
      <span className={`rounded px-1.5 py-0.5 text-[10px] ${PHASE_CLASS[phase]}`}>{PHASE_LABEL[phase]}</span>
      {time ? <span className="text-[10px] text-muted-foreground">{time}</span> : null}
      {phase === 'pre' && trading_day === false ? (
        <span className="text-[10px] text-muted-foreground">非交易日</span>
      ) : null}
    </span>
  )
}

export default ThemeMoodStatus
