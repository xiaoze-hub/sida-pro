import { useState, useEffect } from 'react'
export { cn } from '@panwatch/base-ui'

/**
 * 持久化到 localStorage 的 useState
 * @param key localStorage 键名
 * @param defaultValue 默认值
 */
export function useLocalStorage<T>(key: string, defaultValue: T): [T, (value: T | ((prev: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const saved = localStorage.getItem(key)
      if (saved !== null) {
        return JSON.parse(saved)
      }
    } catch {
      // ignore
    }
    return defaultValue
  })

  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value))
    } catch {
      // ignore
    }
  }, [key, value])

  return [value, setValue]
}

// ==================== 时间格式化工具 ====================

/**
 * 解析后端时间字符串为 Date。
 * 后端时间(PostgreSQL `timestamp without time zone` + func.now(), PG 时区
 * Asia/Shanghai)序列化后是【北京时间的裸字符串】, 无时区标记。
 * 裸字符串直接 new Date() 会被浏览器按本地时间解析 → 正确显示北京时间。
 * 若字符串自带时区标记(Z / ±HH:MM)则按标记解析。
 */
export function parseServerTime(iso?: string | null): Date {
  if (!iso) return new Date(NaN)
  const s = iso.trim()
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)
  if (hasTz) return new Date(s)
  // 无时区标记的裸字符串 = 后端存的是北京时间(naive), 按本地时间解析即可
  return new Date(s)
}

/**
 * 格式化 ISO 时间为本地时间（仅时间）
 * @param isoTime ISO 格式时间字符串
 * @returns 如 "15:30"
 */
export function formatTime(isoTime?: string | null): string {
  if (!isoTime) return ''
  try {
    const date = parseServerTime(isoTime)
    if (isNaN(date.getTime())) return ''
    return date.toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    })
  } catch {
    return ''
  }
}

/**
 * 格式化 ISO 时间为本地日期时间
 * @param isoTime ISO 格式时间字符串
 * @returns 如 "01/26 15:30"
 */
export function formatDateTime(isoTime?: string | null): string {
  if (!isoTime) return ''
  try {
    const date = parseServerTime(isoTime)
    if (isNaN(date.getTime())) return ''
    return date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    })
  } catch {
    return ''
  }
}

/**
 * 格式化 ISO 时间为完整本地日期时间
 * @param isoTime ISO 格式时间字符串
 * @returns 如 "2024-01-26 15:30:00"
 */
export function formatFullDateTime(isoTime?: string | null): string {
  if (!isoTime) return ''
  try {
    const date = parseServerTime(isoTime)
    if (isNaN(date.getTime())) return ''
    return date.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    })
  } catch {
    return ''
  }
}
