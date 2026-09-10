import { describe, expect, it } from 'vitest'
import { formatLatency, trustSummary, trustTone, trustTooltip } from '../../src/lib/vendor-trust'
import type { VendorTrustItem } from '@panwatch/api'

const base: VendorTrustItem = {
  vendor: 'tencent',
  score: 98,
  success_rate: 0.98,
  p50_latency_ms: 400,
  ewma_latency_ms: 512,
  samples: 100,
  last_error: '',
}

describe('trustTone (心跳色档)', () => {
  it('>=80 ok / >=50 warn / <50 bad / 无分 unknown', () => {
    expect(trustTone(98)).toBe('ok')
    expect(trustTone(80)).toBe('ok')
    expect(trustTone(79)).toBe('warn')
    expect(trustTone(50)).toBe('warn')
    expect(trustTone(49)).toBe('bad')
    expect(trustTone(0)).toBe('bad')
    expect(trustTone(null)).toBe('unknown')
    expect(trustTone(undefined)).toBe('unknown')
    expect(trustTone(Number.NaN)).toBe('unknown')
  })
})

describe('formatLatency', () => {
  it('毫秒直读, 秒级保留 1 位; 无效值显式 —', () => {
    expect(formatLatency(512)).toBe('512ms')
    expect(formatLatency(999)).toBe('999ms')
    expect(formatLatency(1200)).toBe('1.2s')
    expect(formatLatency(0)).toBe('0ms')
    expect(formatLatency(null)).toBe('—')
    expect(formatLatency(undefined)).toBe('—')
  })
})

describe('trustTooltip', () => {
  it('含 源名/成功率/EWMA 延迟/p50/样本', () => {
    const s = trustTooltip(base)
    expect(s).toContain('tencent')
    expect(s).toContain('98%')
    expect(s).toContain('512ms')
    expect(s).toContain('400ms')
    expect(s).toContain('100')
  })

  it('无样本(从未调用)不冒充数字; 最近错误截断展示', () => {
    const s = trustTooltip({
      vendor: 'never',
      score: null,
      success_rate: null,
      p50_latency_ms: null,
      ewma_latency_ms: null,
      samples: 0,
      last_error: 'x'.repeat(200),
    })
    expect(s).toContain('never')
    expect(s).toContain('—')
    expect(s).not.toContain('0%')
    expect(s).not.toContain('NaN')
    expect(s.length).toBeLessThan(160)
  })
})

describe('trustSummary', () => {
  it('汇总各档数量', () => {
    const items: VendorTrustItem[] = [
      { ...base, score: 98 },
      { ...base, vendor: 'a', score: 60 },
      { ...base, vendor: 'b', score: 10 },
      { ...base, vendor: 'c', score: null, success_rate: null, samples: 0 },
    ]
    const s = trustSummary(items)
    expect(s).toContain('4 源')
    expect(s).toContain('1 正常')
    expect(s).toContain('2 需关注') // warn+bad 合计 2 项
    expect(s).toContain('1 无样本')
  })

  it('空列表显式"暂无样本"', () => {
    expect(trustSummary([])).toContain('暂无')
  })
})
