/**
 * 会话消息总线(2026-09-22)。
 *
 * 由来: 预警/降级/失败此前只能靠 **toast** —— toast 是一次性的, 错过即永久错过; 而盯盘恰恰需要
 * "回头看一眼刚才那条是什么时候来的"。这里给全站一个**共享**的会话消息流:
 * 任何地方 `pushAlert(...)`, 右栏 `<AlertLog>` 累积展示(带时间戳/级别/口径)。
 *
 * 设计取舍:
 * - **模块级 store + useSyncExternalStore**(不是 Context): 消息来自 API 层/数据源层等非 React 上下文,
 *   用 Context 会逼着这些层拿到 provider;
 * - **同一条消息 30s 内重复只累加计数**, 不刷屏(轮询型接口失败时最容易刷屏);
 * - 容量上限 200(会话级, 不做持久化 —— 这是"本次会话"的日志, 不是审计账本)。
 */
import { useSyncExternalStore } from 'react'

export type AlertLevel = 'info' | 'warn' | 'risk'

export interface AppAlert {
  id: string
  /** 毫秒时间戳 */
  at: number
  level: AlertLevel
  text: string
  /** 数据口径(有则显示徽章; 没有就不显示, 不补造) */
  caliber?: string
  /** 同一消息在去重窗口内出现的次数(1 = 首次) */
  repeat: number
}

const CAP = 200
/** 去重窗口(ms): 同一条消息在这个窗口内重复 → 只累加计数 */
const DEDUPE_MS = 30_000

let alerts: AppAlert[] = []
let seq = 0
const listeners = new Set<() => void>()
const dedupe = new Map<string, { at: number; id: string }>()

function emit() {
  for (const l of listeners) l()
}

/** 订阅(给 useSyncExternalStore 用) */
export function subscribeAlerts(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function getAlerts(): AppAlert[] {
  return alerts
}

export interface PushAlertInput {
  level: AlertLevel
  text: string
  caliber?: string
  /** 去重键; 不传则用 text 本身 */
  key?: string
}

/** 推一条会话消息(自动去重 + 保序 + 截断到 CAP)。 */
export function pushAlert(input: PushAlertInput): void {
  const now = Date.now()
  const key = input.key ?? input.text
  const hit = dedupe.get(key)
  if (hit && now - hit.at < DEDUPE_MS) {
    // 重复消息: 只更新计数与时间(in-place 替换该条, 保持"最近发生"的顺序位置)
    const idx = alerts.findIndex((a) => a.id === hit.id)
    if (idx >= 0) {
      const next = { ...alerts[idx], at: now, repeat: alerts[idx].repeat + 1 }
      alerts = [next, ...alerts.filter((_, i) => i !== idx)]
      dedupe.set(key, { at: now, id: hit.id })
      emit()
      return
    }
    dedupe.delete(key)
  }
  seq += 1
  const item: AppAlert = { id: `al-${seq}`, at: now, level: input.level, text: input.text, caliber: input.caliber, repeat: 1 }
  alerts = [item, ...alerts].slice(0, CAP)
  dedupe.set(key, { at: now, id: item.id })
  emit()
}

export function clearAlerts(): void {
  alerts = []
  dedupe.clear()
  emit()
}

/** 订阅当前会话消息(右栏 `<AlertLog>` 用它)。 */
export function useAlerts(): AppAlert[] {
  return useSyncExternalStore(subscribeAlerts, getAlerts, getAlerts)
}

/** 测试用: 重置 store(用例之间隔离) */
export function __resetAlerts(): void {
  alerts = []
  seq = 0
  dedupe.clear()
  listeners.clear()
}
