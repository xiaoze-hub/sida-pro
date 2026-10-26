import { fetchAPI, getToken, logout } from './client'

export interface SelfCheckItem {
  category: 'system' | 'datasource' | 'ai' | 'notify'
  key: string
  name: string
  status: 'ok' | 'slow' | 'fail'
  latency_ms: number
  error: string | null
  /** 中文修复提示(仅 fail 时非空)。 */
  hint: string
  /** 例如通知"仅校验配置未真发"。 */
  note: string | null
  /** 二级分组(AI 类目=服务商名);其余类目为 null。 */
  group: string | null
}

export interface SelfCheckResult {
  items: SelfCheckItem[]
  summary: {
    total: number
    ok: number
    slow: number
    fail: number
  }
  notify_send: boolean
}

export const healthApi = {
  streamSystemStatus,
  systemStatus: () => fetchAPI<SystemStatusSnapshot>('/health/system', { cacheMode: false, timeoutMs: 10000 }),
  /** 系统自检(数据源/AI/通知连通性)。notifySend=true 会真实发送测试通知。 */
  selfcheck: (notifySend = false) =>
    fetchAPI<SelfCheckResult>('/health/selfcheck', { method: 'POST', body: JSON.stringify({ notify_send: notifySend }), timeoutMs: 60000 }),

  /** 只取可自检项清单(不探测,秒回),用于先渲染再逐项检查。 */
  selfcheckList: () =>
    fetchAPI<{ items: Array<{ category: string; key: string; name: string; group: string | null }> }>(
      '/health/selfcheck?list=1', { cacheMode: false },
    ),

  /** 只探测指定 key 的若干项(用于逐项/小并发自检)。notifySend 仅影响 notify 类目。 */
  selfcheckKeys: (keys: string[], notifySend = false) =>
    fetchAPI<SelfCheckResult>(
      '/health/selfcheck',
      { method: 'POST', body: JSON.stringify({ keys, notify_send: notifySend }), timeoutMs: 30000 },
    ),
}

export type ModuleStatus = 'ok' | 'slow' | 'fail' | 'unknown' | 'disabled' | 'standby' | 'unconfigured' | 'checking' | 'configured'
export interface SystemStatusNode {
  id: string; label: string; kind: string; status: ModuleStatus; detail: string
  connected: boolean; latency_ms: number | null
  items: Array<{ key: string; name: string; status: ModuleStatus; note?: string | null; error?: string | null; hint?: string; latency_ms?: number }>
}
export interface SystemStatusSnapshot {
  checked_at: string; version: string; nodes: SystemStatusNode[]
  edges: Array<{ id: string; source: string; target: string; connected: boolean }>
}

export interface SystemStatusUpdate {
  nodes: SystemStatusNode[]; done_count: number; total: number; checked_at: string
  done?: boolean; version?: string; error?: string
}

async function streamSystemStatus(onUpdate: (update: SystemStatusUpdate) => void, signal: AbortSignal) {
  const token = getToken()
  const response = await fetch('/api/health/system/stream', { credentials: 'include', cache: 'no-store', headers: token ? { Authorization: `Bearer ${token}` } : {}, signal })
  if (response.status === 401) { logout(); throw new Error('登录已过期') }
  if (!response.ok || !response.body) throw new Error(response.status === 403 ? '仅管理员可查看系统状态' : `状态读取失败 HTTP ${response.status}`)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let complete = false
  const dispatch = (raw: string) => {
    const data = raw.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n')
    if (!data) return
    const update = JSON.parse(data) as SystemStatusUpdate
    if (update.error) throw new Error(update.error)
    if (!Array.isArray(update.nodes)) throw new Error('无效的模块状态更新')
    onUpdate(update)
    if (update.done) complete = true
  }
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) { dispatch(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 2); boundary = buffer.indexOf('\n\n') }
      if (done || complete) break
    }
    if (buffer.trim()) dispatch(buffer)
    if (!complete) throw new Error('状态更新中断，保留未更新模块的原有记录')
  } finally { void reader.cancel().catch(() => {}); reader.releaseLock() }
}
