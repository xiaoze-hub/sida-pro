/** 页面即上下文：从页面的 symbol/period 生成上报报文（P1）。 */
import { reportViewState, type ViewStatePayload } from './api'

const lastSent = new Map<string, number>()

/** 同一视图 10s 内不重复上报（K 线页会频繁重渲染）。 */
export function reportViewOnce(payload: ViewStatePayload, key = 'default'): void {
  const now = Date.now()
  const prev = lastSent.get(key) ?? 0
  if (now - prev < 10_000) return
  lastSent.set(key, now)
  void reportViewState(payload).catch(() => {
    // 视图上报是旁路，失败不打扰用户
  })
}
