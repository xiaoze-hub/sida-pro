/**
 * 只读标定的「已生效」口径(2026-11-12 复审)。
 *
 * 红线: **建议 ≠ 已生效**。
 * - 数字只能来自后端返回的 `changed`(真正改了几条生效权重)与 `write_back`
 *   (是否回写)。前端写死 `已生效: 0` 是幻觉风险: 一旦后端改了写入语义,
 *   界面会继续撒谎, 而且没人会发现。
 * - 后端没给这两个字段时按「未生效」显示, 但把原因写成「后端未回传」而不是
 *   「一定是 0」。
 */
export interface EffectiveSummary {
  /** 后端回传的实际生效条数(缺失按 0 处理) */
  changed: number
  /** 后端是否声明回写了生效权重 */
  applied: boolean
  /** 直接可渲染的一行文案 */
  text: string
  /** ok = 与「只读」一致; warn = 出现了与只读预期不符的字段, 界面要显眼 */
  tone: 'ok' | 'warn'
}

export function effectiveSummary(
  calib:
    | { changed?: number | null; write_back?: boolean | null }
    | null
    | undefined,
): EffectiveSummary {
  const rawChanged = calib?.changed
  const hasChanged = typeof rawChanged === 'number' && Number.isFinite(rawChanged)
  const changed = hasChanged ? (rawChanged as number) : 0
  const applied = calib?.write_back === true

  if (!applied) {
    // 只读模式: 显示真实 changed(理论恒 0), 并说明为什么不落库。
    return {
      changed,
      applied: false,
      text: `已生效: ${changed}(只读建议, 不落库)`,
      tone: changed === 0 ? 'ok' : 'warn',
    }
  }
  return {
    changed,
    applied: true,
    text: `已生效: ${changed}(后端已回写生效权重)`,
    tone: 'warn',
  }
}
