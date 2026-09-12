/** 策略参数表单的纯函数(v0.5.78, 借鉴"一文件 + META": 表单由策略自己描述)。
 *
 * 后端 `params` 是唯一事实源(它派生自求值器真读的阈值键), 前端只负责:
 *  ① 显示"当前生效值"(默认值或用户覆盖值); ② 只把**真的改过**的键作为 overrides 发出去。
 */
import type { StrategyParam } from '@panwatch/api'

export function displayValue(p: StrategyParam, overrides: Record<string, number>): number {
  return p.key in overrides ? overrides[p.key] : p.value
}

/** 只回传与默认值不同的键 —— 空对象表示"用策略原值", 后端因此不需要区分"没传"与"传了原值"。 */
export function activeOverrides(
  params: StrategyParam[] | undefined,
  overrides: Record<string, number>,
): Record<string, number> {
  const declared = new Set((params ?? []).map((p) => p.key))
  const out: Record<string, number> = {}
  for (const [k, v] of Object.entries(overrides)) {
    if (declared.has(k) && Number.isFinite(v) && v !== params?.find((p) => p.key === k)?.value) {
      out[k] = v
    }
  }
  return out
}

/** 输入框直接给的是字符串: 非数字/越界一律不覆盖(保留原值), 不静默夹到边界。 */
export function parseParamInput(raw: string, p: StrategyParam): number | null {
  const v = Number(raw)
  if (raw.trim() === '' || !Number.isFinite(v)) return null
  if (v < p.min || v > p.max) return null
  return v
}
