/** 证据块状态 → 视觉/文案（纯函数，可单测）。三态必须区分：
 *  - ok      数据正常
 *  - gated   权限不足（显示「升级解锁」，不是 `--`）
 *  - missing 数据缺失/降级（`--` + 原因）
 */
import type { EvidenceBase, Tone } from './types'
import { TONE_META } from './copy'

export type EvidenceState = 'ok' | 'gated' | 'missing'

export function evidenceState(block: EvidenceBase | null | undefined): EvidenceState {
  if (!block) return 'missing'
  if (block.available) return 'ok'
  if (block.gated) return 'gated'
  return 'missing'
}

export function toneMeta(tone: Tone | null | undefined) {
  return TONE_META[tone ?? 'neutral']
}

/** 意图色条（左侧 3px），按 tone 取令牌，不手调透明度 */
export function toneBar(tone: Tone | null | undefined): string {
  return toneMeta(tone).bar
}

/** 置信度 0~1 → 「x/10」；无效返回 `--`（不补 0，不用 toFixed：R6 门禁） */
export function confidenceOfTen(confidence: number | null | undefined): string {
  if (typeof confidence !== 'number' || !Number.isFinite(confidence)) return '--'
  return `${Math.round(confidence * 10)}/10`
}
