// API 错误分类 (2026-08-17 v0.2.64)
//
// 用途: 根据 Error 对象推测是 timeout / HTTP 5xx / 网络错误, 给出更明确的文案
// B 报告 P1-9: 数据源降级时区分 TIMEOUT / HTTP_5xx / NETWORK
//
// 注: 客户端只能从 Error.message 推测(没 axios fetch 原生), 准确率 80%

export type ApiErrorKind = 'TIMEOUT' | 'HTTP_5xx' | 'HTTP_4xx' | 'NETWORK' | 'UNKNOWN'

export function classifyApiError(e: unknown): ApiErrorKind {
  if (!e) return 'UNKNOWN'
  const msg = String((e as any)?.message || e).toLowerCase()
  // Timeout
  if (msg.includes('timeout') || msg.includes('aborted') || msg.includes('timed out')) {
    return 'TIMEOUT'
  }
  // HTTP 5xx
  if (msg.includes('502') || msg.includes('503') || msg.includes('504') || msg.includes('500') || msg.includes('server error')) {
    return 'HTTP_5xx'
  }
  // HTTP 4xx (client error)
  if (msg.includes('401') || msg.includes('403') || msg.includes('404') || msg.includes('400')) {
    return 'HTTP_4xx'
  }
  // Network
  if (msg.includes('network') || msg.includes('fetch failed') || msg.includes('econnrefused') || msg.includes('enotfound') || msg.includes('dns')) {
    return 'NETWORK'
  }
  return 'UNKNOWN'
}

/**
 * 给 ErrorBanner 一个明确的 message, 根据错误类型给出建议
 */
export function describeApiError(e: unknown): string {
  const kind = classifyApiError(e)
  const raw = (e as any)?.message || '服务不可用'
  switch (kind) {
    case 'TIMEOUT':
      return '请求超时, 请重试或刷新页面'
    case 'HTTP_5xx':
      return '数据源服务不可用, 已自动尝试备用源'
    case 'HTTP_4xx':
      return `请求错误: ${raw.slice(0, 50)}`
    case 'NETWORK':
      return '网络连接失败, 请检查网络后重试'
    default:
      return raw.slice(0, 80)
  }
}