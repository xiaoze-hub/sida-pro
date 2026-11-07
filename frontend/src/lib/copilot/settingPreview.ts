/**
 * 待确认「通知设置」动作的展示口径（纯函数，可单测）。
 *
 * 铁律（2026-11-07 复审阻断项）：确认界面必须念出**本动作**的旧值、新值与三态/风险，
 * 且旧值只认**后端持久化的 propose 基线**（`action.diff`，由
 * `actions._diff_setting` 从 CopilotAction 行的 payload 算出并落库）。
 *
 * 为什么不能读 `action.payload.before` 自己拼文案：payload 是模型提议时的入参容器，
 * 模型完全可以在对话里编一个「原来是 999」；后端在 propose 阶段会用真实基线覆盖它，
 * 但界面没有任何理由去信任它。diff 是后端算完写进库的那一份，才配当"旧值"。
 *
 * 三态（`before_state`）由后端给出：
 *  - absent 无该 key 的行 → 当前生效的是**环境变量默认值**；
 *  - null   有行但没有有效值 → 运行时同样回退到环境变量默认值；
 *  - value  有明确值 → 直接显示。
 *
 * ⚠️ 绝不说「空 = 关闭」：`_build_notifier` 的口径是 `app_settings or env默认`，
 * 空值只会回退默认值。空值现在也在后端被直接拒收。
 *
 * 确认按钮的开关（2026-11-07 独立审查阻断项）：diff 缺失、或 diff 里没有后端算出的
 * `before_state`，界面就**无法核对自己要签的是什么** —— 此时必须 fail closed
 * （`confirmable=false` → 按钮 disabled，点击处理器同样拒绝），而不是「只提示一句、
 * 按钮照旧可点」。老 pending 行（before_state 引入之前落的库）走同一条路径。
 */
export type SettingBeforeState = 'absent' | 'null' | 'value'

export interface SettingDiffEntry {
  field: string
  before: unknown
  after: unknown
  change?: string
  /** 后端算出的三态；老动作行/计算失败时缺失 */
  before_state?: SettingBeforeState
  /** 设置类动作不可撤销，只能重新提议 */
  immutable?: boolean
}

export interface SettingActionLike {
  payload?: Record<string, unknown> | null
  diff?: SettingDiffEntry[] | null
}

export interface SettingPreview {
  key: string
  keyLabel: string
  beforeLabel: string
  afterLabel: string
  state: SettingBeforeState | 'unknown'
  stateLabel: string
  risk: string
  /** 后端没给变更预演（老动作行 / diff 计算失败）：界面必须明说不知道，不许编 */
  missing: boolean
  /** 与当前值相同（点了也不会有变化） */
  noop: boolean
  /**
   * 能否允许用户确认：只有「后端 diff 存在且带 before_state」才为 true。
   * false 时确认按钮必须 disabled，`act()` 里也必须再拒一次（双保险，防绕过）。
   */
  confirmable: boolean
}

/** 三个允许修改的通知设置键 → 中文名（只做标识，不参与任何取值判断） */
export const SETTING_KEY_LABELS: Record<string, string> = {
  notify_quiet_hours: '通知静默时段',
  notify_retry_attempts: '通知失败重试次数',
  notify_retry_backoff_seconds: '通知失败重试退避秒数',
}

const STATE_LABELS: Record<SettingBeforeState, string> = {
  absent: '未配置：当前生效的是环境变量默认值',
  null: '已存在但没有有效值：运行时会回退到环境变量默认值',
  value: '已配置明确值',
}

export const SETTING_RISK = '确认后从下一次运行起生效且不可撤销，只能重新提议修正。空值不会被当成「关闭」（运行时是回退环境变量默认值），后端会直接拒收。'
export const SETTING_MISSING_RISK = '后端没有给出这次变更的旧值预演：不要确认，请忽略后重新提议。'
export const SETTING_UNKNOWN_STATE_RISK = '后端没有标出旧值状态，无法核对此改动：不要确认，请忽略后重新提议。'

function textOf(value: unknown): string {
  if (value === null || value === undefined || value === '') return '--'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return '--'
}

/**
 * 把一条待确认的 setting 动作映射成界面文案。
 *
 * 只读 `action.diff`（后端持久化预演）；`payload` 仅用于显示「哪个键」这一标识符，
 * 其中的 `before` / `before_present` 一律不用 —— 那是可以造假的入参。
 */
export function describeSettingPreview(action: SettingActionLike): SettingPreview {
  const rawKey = action.payload?.key
  const diff = Array.isArray(action.diff) ? action.diff : []
  const entry = diff.find((d) => d && typeof d === 'object') as SettingDiffEntry | undefined
  const key = String(entry?.field ?? (typeof rawKey === 'string' ? rawKey : ''))
  const keyLabel = SETTING_KEY_LABELS[key] ?? (key || '通知设置')

  if (!entry) {
    return {
      key,
      keyLabel,
      beforeLabel: '--',
      afterLabel: '--',
      state: 'unknown',
      stateLabel: '后端未提供变更预演',
      risk: SETTING_MISSING_RISK,
      missing: true,
      noop: false,
      confirmable: false,
    }
  }

  const state = entry.before_state ?? 'unknown'
  return {
    key,
    keyLabel,
    beforeLabel: textOf(entry.before),
    afterLabel: textOf(entry.after),
    state,
    stateLabel: state === 'unknown' ? '后端未标注旧值状态（按未知处理）' : STATE_LABELS[state],
    risk: state === 'unknown' ? SETTING_UNKNOWN_STATE_RISK : SETTING_RISK,
    missing: false,
    noop: entry.change === 'noop',
    confirmable: state !== 'unknown',
  }
}
