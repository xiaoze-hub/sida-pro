/**
 * 前端报错上报 (2026-09-08 系统日志, 对应 POST /api/logs/frontend)。
 *
 * - window.onerror / unhandledrejection 全局兜底 + ErrorBoundary onError 手动调 report()
 * - 同一 message 60s 内只发一次(防白屏刷屏打爆后端)
 * - navigator.sendBeacon 优先(页面崩溃时也能送达), 降级 fetch(keepalive)
 * - 未登录无 token 时直接丢弃(后端要求登录, 不做匿名通道)
 */
import { API_BASE, getToken } from '@panwatch/api'

const ENDPOINT = `${API_BASE}/api/logs/frontend`
const DEDUPE_MS = 60_000
const lastSent = new Map<string, number>()

function fingerprint(type: string, message: string): string {
  return `${type}::${(message || '').slice(0, 200)}`
}

export function reportFrontendError(type: string, message: string, stack?: string): void {
  try {
    const token = getToken()
    if (!token) return
    const fp = fingerprint(type, message)
    const now = Date.now()
    if (now - (lastSent.get(fp) ?? 0) < DEDUPE_MS) return
    lastSent.set(fp, now)
    const body = JSON.stringify({
      type: type.slice(0, 120),
      message: (message || '').slice(0, 500),
      stack: (stack || '').slice(0, 2000),
      url: window.location.href.slice(0, 300),
    })
    const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
    // 注: sendBeacon 发不出 Authorization 头, 后端要求登录, 故只用 fetch(keepalive 页面卸载也能送达)
    void fetch(ENDPOINT, { method: 'POST', headers, body, keepalive: true }).catch(() => {})
  } catch {
    /* 上报本身绝不能抛 */
  }
}

/** main.tsx 启动时调一次。返回清理函数(测试/热更新用)。 */
export function installGlobalErrorReporting(): () => void {
  const onError = (event: ErrorEvent) => {
    reportFrontendError('window.onerror', event.message, event.error?.stack)
  }
  const onRejection = (event: PromiseRejectionEvent) => {
    const r = event.reason as Error | undefined
    reportFrontendError('unhandledrejection', r?.message ?? String(event.reason), r?.stack)
  }
  window.addEventListener('error', onError)
  window.addEventListener('unhandledrejection', onRejection)
  return () => {
    window.removeEventListener('error', onError)
    window.removeEventListener('unhandledrejection', onRejection)
  }
}
