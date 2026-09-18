/**
 * 决策账本的展示口径(单一来源) —— 页面与测试共用, 免得"样本不足"的口径散在 JSX 里。
 *
 * 三条纪律(对齐后端 decision_log + 全仓诚实口径):
 *  ① **样本不足不给数字**: `hit_rate == null || insufficient` → 显示 `--`, 并把原因放进 title;
 *  ② **未回填不是 0**: 收益/命中为 null → `--`(0 是真实收益, 不能顶替"还没有数据");
 *  ③ **不自己算百分比**: 命中率只取后端给的 `hit_rate`, 前端不做任何推算。
 */
import { safeFixed } from '@/lib/format'
import type { DecisionLogItem } from '@panwatch/api'

export interface HorizonLike {
  n: number
  hit_rate: number | null
  insufficient: boolean
  note: string
}

/** 命中率单元格: 有样本给 `xx.x%`, 样本不足给 `--` + 原因。 */
export function hitRateText(h: HorizonLike | undefined): { text: string; muted: boolean; title: string } {
  if (!h) return { text: '--', muted: true, title: '无该档数据' }
  if (h.insufficient || h.hit_rate == null) {
    return { text: '--', muted: true, title: h.note || '样本不足, 不给命中率' }
  }
  return { text: `${safeFixed(h.hit_rate * 100, 1)}%`, muted: false, title: `样本 n=${h.n}` }
}

/** 样本量单元格: 0 也如实显示(0 是真值), 但标灰。 */
export function sampleText(n: number | undefined): string {
  return n == null ? '--' : String(n)
}

/** 收益单元格: 未回填 → `--`; 已回填 → 带符号百分比。 */
export function retText(v: number | null | undefined): string {
  if (v == null) return '--'
  return `${v >= 0 ? '+' : ''}${safeFixed(v * 100, 2)}%`
}

/** 收益的颜色: 未回填走中性色(**不许把 null 当 0 上色**)。 */
export function retClass(v: number | null | undefined): string {
  if (v == null) return 'text-muted-foreground'
  return v >= 0 ? 'text-stock-up' : 'text-stock-down'
}

/** 价格单元格: 当时没取到价 → `--`(不拿今天的价冒充)。 */
export function priceText(v: number | null | undefined): string {
  return v == null ? '--' : safeFixed(v, 2)
}

/** 证据快照摘要: 解析后端存的 JSON, 挑人看得懂的键; 解析失败就原样截断。 */
export function contextSummary(raw: string, max = 46): string {
  const s = (raw || '').trim()
  if (!s) return ''
  try {
    const o = JSON.parse(s) as Record<string, unknown>
    const parts: string[] = []
    for (const [k, v] of Object.entries(o)) {
      if (v == null || typeof v === 'object') continue
      parts.push(`${k}=${v}`)
    }
    const joined = parts.join(' · ')
    return joined.length > max ? `${joined.slice(0, max)}…` : joined
  } catch {
    return s.length > max ? `${s.slice(0, max)}…` : s
  }
}

/** 明细行是否"已回填"(任意一档有结果即算) —— 未回填的行在表里要看得出来。 */
export function isFilled(item: DecisionLogItem): boolean {
  return (['t1', 't3', 't5'] as const).some((k) => item.outcomes?.[k]?.ret != null)
}
