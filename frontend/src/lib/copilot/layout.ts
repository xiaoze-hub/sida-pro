/**
 * 参谋页布局偏好（2026-10-26，体验打磨）。
 *
 * 三件事：左侧会话列宽度可拖、右侧面板可折叠、快捷键。
 * 纯逻辑抽出来是为了能单测 —— 拖拽的夹紧边界写错会让布局塌掉，而 UI 上很难复现。
 */

export const CONV_MIN = 200
export const CONV_MAX = 360
export const CONV_DEFAULT = 256

export const STORAGE_KEYS = {
  convWidth: 'sida.copilot.convWidth',
  panelOpen: 'sida.copilot.panelOpen',
  railOpen: 'sida.copilot.railOpen',
} as const

/** 左栏（对话列表）折叠状态键：'0' = 收起，缺省 = 展开。 */
export const RAIL_KEY = STORAGE_KEYS.railOpen

/** 读布尔偏好（缺省 true）：只有明确 '0'/'false'/'off' 才算关。 */
export function readBoolPref(
  storage: Pick<Storage, 'getItem'> | null | undefined,
  key: string,
): boolean {
  try {
    return parseBoolPref(storage?.getItem(key))
  } catch {
    return true
  }
}

/** 宽度夹紧：非数/NaN 回默认值，绝不把布局拖成 0 或负数。 */
export function clampConvWidth(raw: unknown): number {
  // 空串/null 是"没设过"，回默认；不能因为 Number('')===0 就被夹成最小值
  if (raw === null || raw === undefined || raw === '') return CONV_DEFAULT
  const n = typeof raw === 'number' ? raw : Number(raw)
  if (!Number.isFinite(n)) return CONV_DEFAULT
  return Math.min(CONV_MAX, Math.max(CONV_MIN, Math.round(n)))
}

/** 读布尔偏好：只有明确的 '0'/'false' 才算关，其余（含缺省）按开。 */
export function parseBoolPref(raw: string | null | undefined): boolean {
  const v = String(raw ?? '').trim().toLowerCase()
  if (v === '0' || v === 'false' || v === 'off') return false
  return true
}

export function readConvWidth(storage?: Pick<Storage, 'getItem'> | null): number {
  try {
    return clampConvWidth(storage?.getItem(STORAGE_KEYS.convWidth))
  } catch {
    return CONV_DEFAULT
  }
}

export function readPanelOpen(storage?: Pick<Storage, 'getItem'> | null): boolean {
  try {
    return parseBoolPref(storage?.getItem(STORAGE_KEYS.panelOpen))
  } catch {
    return true
  }
}

export function writePref(
  storage: Pick<Storage, 'setItem'> | null | undefined,
  key: string,
  value: string,
): void {
  try {
    storage?.setItem(key, value)
  } catch {
    // 隐私模式/配额满：偏好丢就丢了，不能因此让页面报错
  }
}

export interface ShortcutContext {
  /** 是否正在输入（contenteditable / input / textarea） */
  inEditable: boolean
  /** 是否还有流式任务在跑 */
  streaming: boolean
}

export type ShortcutAction = 'send' | 'new-conversation' | 'toggle-panel' | null

/**
 * 快捷键判定（返回动作，不直接执行 —— 便于测试与后续改键）。
 *
 * - Cmd/Ctrl + Enter：发送（输入法组合中不触发）
 * - Cmd/Ctrl + K：新建会话
 * - Cmd/Ctrl + \ ：折叠/展开右面板
 * 其余一律 null，不拦截浏览器默认行为。
 */
export function resolveShortcut(
  ev: {
    key: string
    metaKey?: boolean
    ctrlKey?: boolean
    altKey?: boolean
    shiftKey?: boolean
    isComposing?: boolean
  },
  ctx: ShortcutContext,
): ShortcutAction {
  if (ev.isComposing) return null
  const mod = Boolean(ev.metaKey || ev.ctrlKey)
  if (!mod || ev.altKey) return null
  const key = String(ev.key ?? '').toLowerCase()

  if (key === 'enter') {
    // 只有焦点在输入区时才当"发送"，否则留给页面其它 Enter 语义
    return ctx.inEditable && !ctx.streaming ? 'send' : null
  }
  if (key === 'k') return 'new-conversation'
  if (key === '\\') return 'toggle-panel'
  return null
}
