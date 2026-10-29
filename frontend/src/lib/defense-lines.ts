/**
 * 防线变更广播（2026-10-29 复审 B5）。
 *
 * ## 为什么需要
 * `DefenseLineBanner` 挂在个股工作台上，而防线/持仓可以在**别的地方**被改掉：
 *   - 持仓页删除该持仓（`DELETE /positions/{id}`）；
 *   - 同一工作台在另一个浏览器标签页里改防线（`PUT /positions/{id}/defense`）；
 *   - 助手/脚本通过 API 改（没有前端事件）。
 * 旧实现只在**挂载时**取一次 `/portfolio/summary`，于是告警条拿着旧防线继续报
 * 「已击穿」，用户按一个已经不存在的纪律位执行操作。
 *
 * ## 三条通道（各自覆盖不同的"别处"）
 *  1. 模块级订阅：同页多个挂载实例（同一 JS 执行上下文）；
 *  2. window `CustomEvent`：同文档里的其他监听方（嵌入页/微前端）；
 *  3. `localStorage` + `storage` 事件：**其他浏览器标签页**（`storage` 事件只在别的
 *     标签页触发，正好是我们要的那一半；本页自己的写入不会回调自己，避免重复）。
 *
 * 通道只广播"有人改了，去重取"，**不携带防线数值**：谁也不知道别的页面改成了什么，
 * 自己猜一个数字就是第二个真相源。
 * 定期续期（`DEFENSE_QUOTE_REFRESH_MS`）依旧是兜底 —— 助手/脚本直接改库不会发任何事件。
 */

/** window 事件名（同文档广播用）。 */
export const DEFENSE_LINES_CHANGED_EVENT = 'sida:defense-lines-changed'

/** 跨标签页通道的 storage key（值只用于让 `storage` 事件有变化，不承载业务数据）。 */
export const DEFENSE_LINES_STORAGE_KEY = 'sida_defense_lines_rev'

type Listener = () => void

const listeners = new Set<Listener>()
/** 本地修订号：保证 storage 写入的值**每次不同**（同值写入不触发 storage 事件）。 */
let revision = 0

/** 同页订阅。返回退订函数（组件卸载时必须调用）。 */
export function onDefenseLinesChanged(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/**
 * 广播"防线/持仓行变了"。**幂等、无参**：订阅方自己重取，不接收数值。
 */
export function notifyDefenseLinesChanged(): void {
  const snapshot = [...listeners]
  for (const listener of snapshot) {
    try {
      listener()
    } catch {
      // 一个订阅方出错不能拦住其他订阅方（告警条不响比抛异常更危险）
    }
  }
  try {
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
      window.dispatchEvent(new CustomEvent(DEFENSE_LINES_CHANGED_EVENT))
    }
  } catch {
    /* 非浏览器环境忽略 */
  }
  try {
    // 跨标签页：写入一个递增的"修订号"（内容无业务含义，只是让 storage 事件必然触发）。
    revision += 1
    window.localStorage.setItem(DEFENSE_LINES_STORAGE_KEY, `${Date.now()}:${revision}`)
  } catch {
    /* 隐私模式/无 localStorage 时退化成同页广播 */
  }
}

/**
 * 监听"别处改了防线"：window 事件（同文档）+ `storage` 事件（其他标签页）。
 * 返回退订函数。
 */
export function onWindowDefenseLinesChanged(listener: Listener): () => void {
  if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') {
    return () => {}
  }
  const handler = () => listener()
  const storageHandler = (event: StorageEvent) => {
    // `storage` 只在**其他**标签页触发；仍按 key 过滤，避免无关写入引起重取。
    if (event.key !== null && event.key !== DEFENSE_LINES_STORAGE_KEY) return
    listener()
  }
  window.addEventListener(DEFENSE_LINES_CHANGED_EVENT, handler)
  window.addEventListener('storage', storageHandler)
  return () => {
    window.removeEventListener(DEFENSE_LINES_CHANGED_EVENT, handler)
    window.removeEventListener('storage', storageHandler)
  }
}
