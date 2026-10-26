import type { SelfCheckItem, SystemStatusNode, SystemStatusSnapshot } from '@panwatch/api'

/** 只把主动探测视为连通证据；通知配置校验、未知项和历史统计不能点亮线路。 */
export function withProbeResults(snapshot: SystemStatusSnapshot, checks: SelfCheckItem[]): SystemStatusSnapshot {
  const nodes = snapshot.nodes.map((node): SystemStatusNode => {
    const items = checks.filter(item => node.id === 'datasource' || node.id === 'ai' || node.id === 'notify'
      ? item.category === node.id : node.id === 'database' ? item.key === 'sys:db' : false)
    if (!items.length) return node
    const failed = items.filter(item => item.status === 'fail').length
    const slow = items.some(item => item.status === 'slow')
    const configurationOnly = node.id === 'notify' && items.every(item => !item.error && item.note?.includes('未真实发送'))
    const status = failed === items.length ? 'fail' : failed || slow ? 'slow' : configurationOnly ? 'configured' : 'ok'
    return { ...node, status, latency_ms: node.id === 'database' ? (status === 'fail' ? null : items[0].latency_ms) : node.latency_ms, connected: status === 'ok' || (status === 'slow' && !failed && !configurationOnly),
      detail: configurationOnly ? '配置格式已校验 · 未检测投递连通' : `主动检测 ${items.length} 项 · ${failed ? `${failed} 项异常` : slow ? '连接较慢' : '连接正常'}`,
      items: items.map(item => ({ ...item })) }
  })
  const byId = new Map(nodes.map(node => [node.id, node]))
  return { ...snapshot, nodes, edges: snapshot.edges.map(edge => ({ ...edge, connected: !!byId.get(edge.source)?.connected && !!byId.get(edge.target)?.connected })) }
}

export const MODULE_STATUS_LABELS = { ok: '正常', slow: '需关注', fail: '未连通', unknown: '未检测', disabled: '未启用', standby: '待命', unconfigured: '未配置', checking: '检测中', configured: '已校验' } as const

export function emptySystemStatus(): SystemStatusSnapshot {
  const modules = [['api', 'SIDA 应用服务', 'api'], ['database', '数据库', 'database'], ['redis', 'Redis 缓存', 'redis'], ['biz_cache', '业务缓存', 'cache'], ['datasource', '行情与数据源', 'datasource'], ['scheduler', '任务调度', 'scheduler'], ['ai', 'AI 分析模型', 'ai'], ['forecast_engine', '预测引擎', 'forecast_engine'], ['notify', '通知渠道', 'notify']]
  return { checked_at: '', version: '--', nodes: modules.map(([id, label, kind]) => ({ id, label, kind, status: 'unknown', detail: '等待读取状态', connected: false, latency_ms: null, items: [] })),
    edges: [['datasource', 'api'], ['api', 'database'], ['api', 'biz_cache'], ['biz_cache', 'redis'], ['scheduler', 'api'], ['api', 'ai'], ['api', 'forecast_engine'], ['api', 'notify']].map(([source, target]) => ({ id: `${source}-${target}`, source, target, connected: false })) }
}

function cacheKey() {
  try { const id = JSON.parse(localStorage.getItem('user') || '{}').id; return typeof id === 'string' && id ? `sida_system_status_v1:${id}` : null } catch { return null }
}
export function readSystemStatusCache(): SystemStatusSnapshot | null {
  try {
    const key = cacheKey()
    if (!key) return null
    const cached = JSON.parse(sessionStorage.getItem(key) || 'null') as SystemStatusSnapshot | null
    if (!cached || !Array.isArray(cached.nodes) || !Array.isArray(cached.edges) || !Number.isFinite(Date.parse(cached.checked_at))) return null
    const expected = emptySystemStatus()
    if (cached.nodes.length !== expected.nodes.length || !expected.nodes.every(node => cached.nodes.some(item => item.id === node.id && item.label && item.status in MODULE_STATUS_LABELS))) return null
    // 缓存只用于先展示历史记录；必须等本轮验证后才允许线路流动。
    return { ...cached, nodes: cached.nodes.map(node => ({ ...node, connected: false })), edges: cached.edges.map(edge => ({ ...edge, connected: false })) }
  } catch { return null }
}
export function saveSystemStatusCache(data: SystemStatusSnapshot) {
  try { const key = cacheKey(); if (key) sessionStorage.setItem(key, JSON.stringify(data)) } catch { /* 禁用存储时仍可正常刷新 */ }
}
