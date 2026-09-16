/**
 * 前端统一 logger (2026-09-15 安全审计 P2)
 *
 * - 开发环境: 透传 console, 便于调试
 * - 生产环境: 剥离 payload — Error 只保留 message, 其它对象不落盘
 *   (避免把 API 响应体/用户数据/堆栈打进浏览器控制台)
 *
 * 用法: 用 logger.error/warn 替换裸 console.error/warn(e)
 */
const isProd = typeof import.meta !== 'undefined' && !!import.meta.env?.PROD

function sanitizeArgs(args: unknown[]): unknown[] {
  if (!isProd) return args
  return args.map((a) => {
    if (a instanceof Error) return a.message
    if (typeof a === 'string') return a
    if (a == null) return a
    return '[redacted]'
  })
}

export const logger = {
  debug: (...args: unknown[]) => {
    console.debug(...sanitizeArgs(args))
  },
  info: (...args: unknown[]) => {
    console.info(...sanitizeArgs(args))
  },
  warn: (...args: unknown[]) => {
    console.warn(...sanitizeArgs(args))
  },
  error: (...args: unknown[]) => {
    console.error(...sanitizeArgs(args))
  },
}

export default logger
