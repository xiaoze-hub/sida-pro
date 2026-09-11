/** 题材情绪分纯函数(2026-09-12): 色阶/格式化/窗口切分, 供页面与测试复用。 */
import { safeFixed } from '@/lib/format'

export interface MoodCell {
  date: string
  score: number | null
  limit_up_cnt: number | null
}

export function fmtScore(v: number | null | undefined): string {
  if (v == null) return '--'
  return safeFixed(v, 1)
}

export function cellColorClass(score: number | null | undefined): string {
  if (score == null || !Number.isFinite(score)) return 'bg-muted/30'
  if (score < 50) return 'bg-muted/40'
  if (score < 60) return 'bg-muted/60'
  if (score < 70) return 'bg-stock-up/15'
  if (score < 82) return 'bg-stock-up/30'
  return 'bg-stock-up/45'
}

export function cellTextClass(score: number | null | undefined): string {
  if (score != null && Number.isFinite(score) && score >= 60) return 'text-stock-up font-medium'
  return 'text-muted-foreground'
}

export function splitWindows(cells: MoodCell[], window: number): MoodCell[] {
  if (!Number.isFinite(window) || window <= 0) return []
  return cells.slice(Math.max(0, cells.length - window))
}
