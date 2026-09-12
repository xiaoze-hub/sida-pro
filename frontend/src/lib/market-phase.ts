/** 市场情绪周期(6 阶段)前端纯函数(2026-09-12, 借鉴 TSP 阶段体系)。
 *
 * 配色与 packages/biz-ui MarketPhaseCard 的 PHASE_STYLE 保持一致(同一套语义色),
 * 这里只取"色带/圆点"两档, 供时间轴与徽标复用。
 */

export type PhaseKey =
  | 'ice'
  | 'ignite'
  | 'rally'
  | 'climax'
  | 'ebb'
  | 'repair'
  | 'accumulating'

export interface PhaseSegment {
  phase: PhaseKey
  label: string
  start: string
  end: string
  days: number
  avg_height: number
  avg_first_board: number
  avg_ge2: number
  avg_promo: number | null
  avg_seal_rate: number | null
}

export interface PhaseStats {
  count: number
  total_days: number
  max_days: number
  avg_days: number
  label: string
  next: Record<string, number>
  next_labels: Record<string, number>
}

export const PHASE_LABEL: Record<PhaseKey, string> = {
  ice: '冰点',
  ignite: '启动',
  rally: '主升',
  climax: '高潮',
  ebb: '退潮',
  repair: '修复',
  accumulating: '积累中',
}

/** 阶段 → 色带/圆点配色(与 biz-ui PHASE_STYLE.dot 同源)。 */
export const PHASE_BAND: Record<PhaseKey, string> = {
  ice: 'bg-blue-500/70',
  ignite: 'bg-cyan-500/70',
  rally: 'bg-red-500/70',
  climax: 'bg-red-900/80',
  ebb: 'bg-orange-500/70',
  repair: 'bg-gray-500/45',
  accumulating: 'bg-slate-500/40',
}

export const PHASE_TEXT: Record<PhaseKey, string> = {
  ice: 'text-blue-600 dark:text-blue-400',
  ignite: 'text-cyan-600 dark:text-cyan-400',
  rally: 'text-red-600 dark:text-red-400',
  climax: 'text-red-800 dark:text-red-300',
  ebb: 'text-orange-600 dark:text-orange-400',
  repair: 'text-gray-600 dark:text-gray-400',
  accumulating: 'text-slate-600 dark:text-slate-400',
}

export function phaseBandClass(phase: string | undefined | null): string {
  return PHASE_BAND[(phase as PhaseKey) ?? 'accumulating'] ?? PHASE_BAND.accumulating
}

export function phaseTextClass(phase: string | undefined | null): string {
  return PHASE_TEXT[(phase as PhaseKey) ?? 'accumulating'] ?? PHASE_TEXT.accumulating
}

/** 当前阶段的"规律": 历史段数/平均/最长 + 去向概率(取 next_labels 前 3)。 */
export function currentPhaseRule(
  current: PhaseKey | null,
  stats: Record<string, PhaseStats> | null,
): { count: number; avg_days: number; max_days: number; next: [string, number][] } | null {
  if (!current || !stats) return null
  const st = stats[current]
  if (!st) return null
  return {
    count: st.count,
    avg_days: st.avg_days,
    max_days: st.max_days,
    next: Object.entries(st.next_labels).slice(0, 3),
  }
}
