/**
 * 路由切换关掉遗留弹层（2026-10-24，Lane B）
 *
 * 背景 bug：工作台是「多标签页保活」结构，页面组件切走后**不卸载**，
 * 而弹窗由 Dialog 挂到 body 的 portal 上。结果：在 `/notifications?tab=alerts`
 * 打开的「新建提醒规则」切到 `/admin` 后仍在，40% 遮罩盖住整页，看起来像应用卡死。
 *
 * 修法：路由变化时统一派发一次 Escape —— Radix 的 DismissableLayer 在 document 上
 * 监听 Escape 并回调 `onOpenChange(false)`，因此所有打开的弹层会被同一事件关掉。
 * 根因（portal 未挂到路由子树）留待 base-ui 统一改造，这里先止血。
 */

/**
 * 关闭当前所有遗留弹层。
 * @returns 被触发关闭的弹层数量（0 表示没有弹层，未派发事件）
 */
export function closeOpenDialogs(root: Document = document): number {
  const dialogs = root.querySelectorAll('[role="dialog"], [role="alertdialog"]')
  if (dialogs.length === 0) return 0
  root.dispatchEvent(
    new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true, cancelable: true }),
  )
  return dialogs.length
}
