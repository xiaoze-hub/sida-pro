/**
 * 首页「证据层」折叠状态（2026-10-24 首屏重排）。
 *
 * 为什么不是全局一个开关：L2-A 那版是「一个按钮放/收 7 个模块」——
 * 收起时把结论的支撑（情绪阶段、主线）藏了，展开时又把异动池 20 多个数字一次倒出来。
 * 卡片化的教训已经证明「全有或全无」不成立，折叠要**按块**、要**记状态**。
 *
 * 迁移：老用户 `sida_dashboard_more=1`（曾经点过"展开更多"）→ 各块默认展开，
 * 不能因为升级就把他习惯看的东西全收起来。
 */
export const DASH_COLLAPSE_PREFIX = 'sida_dash_collapse:'
const LEGACY_MORE_KEY = 'sida_dashboard_more'

function legacyExpanded(): boolean {
  try {
    return localStorage.getItem(LEGACY_MORE_KEY) === '1'
  } catch {
    return false
  }
}

/** 未存过时用 defaultCollapsed；老用户点过"展开更多"则一律视为展开 */
export function readCollapsed(id: string, defaultCollapsed = true): boolean {
  try {
    const raw = localStorage.getItem(DASH_COLLAPSE_PREFIX + id)
    if (raw === '1') return true
    if (raw === '0') return false
  } catch {
    /* localStorage 不可用：走默认 */
  }
  return defaultCollapsed && !legacyExpanded()
}

export function writeCollapsed(id: string, collapsed: boolean): void {
  try {
    localStorage.setItem(DASH_COLLAPSE_PREFIX + id, collapsed ? '1' : '0')
  } catch {
    /* 写不进去也不能影响渲染 */
  }
}
