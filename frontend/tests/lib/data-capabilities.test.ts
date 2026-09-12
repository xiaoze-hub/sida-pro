import { describe, expect, it } from 'vitest'
import {
  capabilitySummary,
  effectiveSource,
  sortItemsByRisk,
  type CapabilityItem,
  type CapabilitySummary,
} from '@/lib/data-capabilities'

const item = (type: string, label: string, status: CapabilityItem['status'], sources: CapabilityItem['sources'] = []): CapabilityItem => ({
  type, label, status, status_label: status, reason: '', sources,
  enabled_count: sources.filter((s) => s.enabled).length, latest_date: null, age_days: null,
})

const src = (provider: string, enabled = true, priority = 1, success_rate: number | null = null): CapabilityItem['sources'][number] => ({
  provider, name: provider, enabled, priority, success_rate, samples: 20, ewma_latency_ms: null, last_error: '',
})

const summary = (over: Partial<CapabilitySummary> = {}): CapabilitySummary => ({
  total: 15, ok: 12, degraded: 2, unknown: 1, unavailable: 1,
  degraded_labels: ['龙虎榜', '北向资金'], unavailable_labels: ['融资融券'], ...over,
})

describe('capabilitySummary', () => {
  it('全正常只显示计数', () => {
    expect(capabilitySummary(summary({ degraded: 0, unavailable: 0, degraded_labels: [], unavailable_labels: [] })))
      .toBe('数据能力 12/15')
  })
  it('有异常时列出前两项并给总数', () => {
    const t = capabilitySummary(summary())
    expect(t).toContain('数据能力 12/15')
    expect(t).toContain('融资融券、龙虎榜')   // 无可用源排在降级之前
    expect(t).toContain('等3项')
  })
  it('无数据返回空串', () => {
    expect(capabilitySummary(undefined)).toBe('')
  })
})

describe('sortItemsByRisk', () => {
  it('无可用源 > 降级 > 未测量 > 正常', () => {
    const sorted = sortItemsByRisk([
      item('k', 'K线数据', 'ok'), item('d', '分红', 'degraded'),
      item('u', '北向资金', 'unavailable'), item('n', '快讯', 'unknown'),
    ])
    expect(sorted.map((i) => i.status)).toEqual(['unavailable', 'degraded', 'unknown', 'ok'])
  })
})

describe('effectiveSource', () => {
  it('取启用源里 priority 最小者; 全禁用返回 null', () => {
    const it = item('q', '实时行情', 'ok', [src('tq', true, 4, 0.9), src('tencent', true, 1, 0.7), src('x', false, 0, 1)])
    expect(effectiveSource(it)?.provider).toBe('tencent')
    expect(effectiveSource(item('q', '实时行情', 'unavailable', [src('a', false)]) as CapabilityItem)).toBeNull()
  })
})
