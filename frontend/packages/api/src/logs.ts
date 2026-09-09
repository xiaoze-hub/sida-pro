import { fetchAPI } from './client'

/** 错误追踪事件(KI-018): 后端未处理异常 + scheduler + 前端上报, owner 可见。 */
export interface ErrorEvent {
  ts?: string
  type?: string
  message?: string
  traceback?: string
  context?: Record<string, unknown> | null
}

export interface ErrorEventsResp {
  items: ErrorEvent[]
}

export const logsApi = {
  /** 最近错误事件(owner)。limit 1..200。 */
  errors: (limit = 50) =>
    fetchAPI<ErrorEventsResp>(`/logs/errors?limit=${encodeURIComponent(String(limit))}`),
}
