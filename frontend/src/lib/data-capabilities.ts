/** 数据集能力矩阵的纯函数与配色(2026-09-12, 借鉴 tick-stock-panel 能力路由视图)。
 *
 * 配色约定(对齐"涨跌色只用于价格"): 正常=emerald, 降级=amber, 无可用源=red,
 * 未测量=灰(刻意不与"正常"同色, 避免把没测过看成没问题)。
 */

export type CapabilityStatus = 'ok' | 'degraded' | 'unknown' | 'unavailable'

export interface CapabilitySource {
  provider: string
  name: string | null
  enabled: boolean
  priority: number | null
  success_rate: number | null
  samples: number | null
  /** 读数依据: ewma=进程内滚动窗口(重启归零) / db=累计计数 / none=两者都不足 */
  basis?: 'ewma' | 'db' | 'none'
  ewma_latency_ms: number | null
  last_error: string
}

/** 驱动本次结论的那个源(后端 classify 用的就是它, 与"按 priority 路由生效"是两回事)。 */
export interface CapabilityVerdict {
  provider: string
  success_rate: number
  samples: number
  basis: 'ewma' | 'db' | 'none'
}

export interface CapabilityItem {
  type: string
  label: string
  status: CapabilityStatus
  status_label: string
  reason: string
  sources: CapabilitySource[]
  /** 未测量/无可用源时为 null —— 此时**不许**显示成功率。 */
  verdict: CapabilityVerdict | null
  enabled_count: number
  latest_date: string | null
  age_days: number | null
}

export interface CapabilitySummary {
  total: number
  ok: number
  degraded: number
  unknown: number
  unavailable: number
  degraded_labels: string[]
  unavailable_labels: string[]
}

export interface CapabilitiesResp {
  items: CapabilityItem[]
  summary: CapabilitySummary
  /** 判定门槛(后端 MIN_SAMPLES 透传): 样本不足时 UI 显示进度而不是结论。 */
  min_samples?: number
}

export const STATUS_DOT: Record<CapabilityStatus, string> = {
  ok: 'bg-emerald-500',
  degraded: 'bg-amber-500',
  unknown: 'bg-gray-400',
  unavailable: 'bg-red-500',
}

export const STATUS_TEXT: Record<CapabilityStatus, string> = {
  ok: 'text-emerald-600 dark:text-emerald-400',
  degraded: 'text-amber-600 dark:text-amber-400',
  unknown: 'text-muted-foreground',
  unavailable: 'text-red-600 dark:text-red-400',
}

/** 侧栏一行摘要: "数据能力 12/15"; 有异常时附前两个降级/缺失项。 */
export function capabilitySummary(s: CapabilitySummary | undefined): string {
  if (!s) return ''
  const bad = [...s.unavailable_labels, ...s.degraded_labels]
  const head = `数据能力 ${s.ok}/${s.total}`
  return bad.length ? `${head} · ${bad.slice(0, 2).join('、')}${bad.length > 2 ? ` 等${bad.length}项` : ''}` : head
}

/** 排序: 有问题的排前面(无可用源 → 降级 → 未测量 → 正常), 同级按名称。 */
export function sortItemsByRisk(items: CapabilityItem[]): CapabilityItem[] {
  const rank: Record<CapabilityStatus, number> = { unavailable: 0, degraded: 1, unknown: 2, ok: 3 }
  return [...items].sort((a, b) => rank[a.status] - rank[b.status] || a.label.localeCompare(b.label, 'zh'))
}

/** 生效源 = 启用源里 priority 最小者(与运行期兜底顺序一致); 无启用源返回 null。 */
export function effectiveSource(item: CapabilityItem): CapabilitySource | null {
  const on = item.sources.filter((s) => s.enabled)
  if (!on.length) return null
  return on.reduce((best, s) =>
    (s.priority ?? 99) < (best.priority ?? 99) ? s : best, on[0])
}

/** 矩阵"读数"列(KI-050): 有结论就显示**驱动结论那个源**的成功率; 没结论就显示
 * 样本进度而不是百分比 —— 绝不允许出现"未测量 · 100%"或"正常 · --"这种自相矛盾。 */
export function capabilityRateCell(item: CapabilityItem, minSamples?: number): string {
  if (item.verdict) return `${Math.round(item.verdict.success_rate * 100)}%`
  if (item.status === 'unavailable') return ''
  if (!minSamples) return ''
  const n = Math.max(0, ...item.sources.filter((s) => s.enabled).map((s) => s.samples ?? 0))
  return `样本 ${n}/${minSamples}`
}
