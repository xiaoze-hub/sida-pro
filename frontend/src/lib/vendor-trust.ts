/**
 * vendor 质量分展示纯函数 (C1, 2026-09-10):
 * /datasources/trust 的 分数/延迟 读数 → 心跳条色档 + tooltip 文案 + 汇总。
 * 口径: 无样本(score/success_rate/延迟为 null)一律显式"未知/—", 绝不冒充数字。
 */
import type { VendorTrustItem } from '@panwatch/api'

import { safeFixed } from './format'

export type TrustTone = 'ok' | 'warn' | 'bad' | 'unknown'

/** 色档 → 圆点类(心跳条/数据源页共用; 与 DataSources HEALTH_DOT 同色系) */
export const TRUST_TONE_CLASS: Record<TrustTone, string> = {
  ok: 'bg-emerald-500',
  warn: 'bg-amber-500',
  bad: 'bg-red-500',
  unknown: 'bg-gray-400',
}

/** 质量分(0-100) → 心跳色档: >=80 正常 / >=50 警示 / <50 差 / 无分未知 */
export function trustTone(score: number | null | undefined): TrustTone {
  if (score === null || score === undefined || !Number.isFinite(score)) return 'unknown'
  if (score >= 80) return 'ok'
  if (score >= 50) return 'warn'
  return 'bad'
}

/** 延迟毫秒 → 文案: 亚秒 "512ms" / 秒级 "1.2s"; 无效值显式 "—" */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${safeFixed(ms / 1000, 1)}s`
}

/** 单源心跳 tooltip: 源名 · 成功率 · 延迟 EWMA · p50 · 样本 · 最近错误(截断) */
export function trustTooltip(item: VendorTrustItem): string {
  const parts = [item.vendor]
  if (item.success_rate !== null && item.success_rate !== undefined) {
    parts.push(`成功率 ${Math.round(item.success_rate * 100)}%`)
  }
  parts.push(`延迟 ${formatLatency(item.ewma_latency_ms)}(EWMA)`)
  if (item.p50_latency_ms !== null && item.p50_latency_ms !== undefined) {
    parts.push(`p50 ${formatLatency(item.p50_latency_ms)}`)
  }
  if (item.samples) parts.push(`样本 ${item.samples}`)
  if (item.last_error) parts.push(`最近错误 ${item.last_error.slice(0, 60)}`)
  return parts.join(' · ')
}

/** 汇总一行: "8 源 · 6 正常 · 1 需关注 · 1 无样本"; 空列表显式"暂无样本" */
export function trustSummary(items: VendorTrustItem[]): string {
  if (items.length === 0) return '暂无样本'
  let ok = 0
  let attention = 0
  let unknown = 0
  for (const it of items) {
    const tone = trustTone(it.score)
    if (tone === 'ok') ok += 1
    else if (tone === 'unknown') unknown += 1
    else attention += 1
  }
  const parts = [`${items.length} 源`, `${ok} 正常`]
  if (attention > 0) parts.push(`${attention} 需关注`)
  if (unknown > 0) parts.push(`${unknown} 无样本`)
  return parts.join(' · ')
}
