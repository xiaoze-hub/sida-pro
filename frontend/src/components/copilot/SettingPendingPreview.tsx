/**
 * 待确认「通知设置」动作的确认前预览（旧值 → 新值 / 三态 / 风险）。
 *
 * 2026-11-07 复审阻断项：旧界面只显示一个 key（`notify_quiet_hours`），等于让用户
 * 确认一件他看不见的事。这里把后端持久化的 diff 明明白白摊开：
 *   旧值（三态：未配置 / 有行但无有效值 / 明确值）→ 新值，并写明"不可撤销"。
 *
 * 取值只走 `@/lib/copilot/settingPreview`（只信后端 diff），组件不做任何自行推断。
 */
import { describeSettingPreview, type SettingActionLike } from '@/lib/copilot/settingPreview'

export function SettingPendingPreview({ action }: { action: SettingActionLike }) {
  const preview = describeSettingPreview(action)
  return (
    <span className="flex flex-col gap-0.5" data-testid="setting-pending-preview">
      <span className="flex flex-wrap items-center gap-1">
        <span className="font-mono" data-testid="setting-pending-key">{preview.keyLabel}</span>
        <span className="text-muted-foreground">旧值</span>
        <span className="font-mono" data-testid="setting-pending-before">{preview.beforeLabel}</span>
        <span aria-hidden="true" className="text-muted-foreground">→</span>
        <span className="text-muted-foreground">新值</span>
        <span className="font-mono" data-testid="setting-pending-after">{preview.afterLabel}</span>
      </span>
      <span className="text-[12px] text-muted-foreground" data-testid="setting-pending-state">
        {preview.stateLabel}
        {preview.noop ? '（与当前值相同）' : ''}
      </span>
      <span
        className={`text-[12px] ${preview.missing ? 'text-role-risk' : 'text-role-watch'}`}
        data-testid="setting-pending-risk"
        role="note"
      >
        {preview.risk}
        {preview.confirmable ? '' : '（确认按钮已禁用）'}
      </span>
    </span>
  )
}

export default SettingPendingPreview
