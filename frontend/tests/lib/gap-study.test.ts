import { describe, expect, it } from 'vitest'
import { bucketForGap, chaseBadge, type GapBucket, type GapStudyResp } from '@/lib/gap-study'

const bucket = (over: Partial<GapBucket> = {}): GapBucket => ({
  bucket: '大幅高开', gap_min: 5, gap_max: 7,
  day: { n: 320, mean: -1.42, median: -1.1, win_rate: 0.31, std: 3.2, t: -7.7 },
  next: { n: 318, mean: -2.6, median: -2.2, win_rate: 0.28, std: 4.1, t: -11.2 },
  halves: {
    first: { n: 160, mean: -1.2, median: -1, win_rate: 0.33, std: 3, t: -5 },
    second: { n: 160, mean: -1.6, median: -1.3, win_rate: 0.29, std: 3.4, t: -5.9 },
  },
  enough_samples: true, stable: true, verdict: 'negative', ...over,
})

const study = (buckets: GapBucket[]): GapStudyResp => ({
  available: true, window_days: 250, since: '2025-01-01', latest_date: '2026-09-11',
  universe: { symbols: 168, observations: 30000, note: '样本=已入库日线的标的, 非全 A 股' },
  excluded_limit_up: 412, min_samples: 60, buckets,
})

describe('bucketForGap', () => {
  const bs = [bucket()]
  it('左闭右开', () => {
    expect(bucketForGap(bs, 5)?.bucket).toBe('大幅高开')
    expect(bucketForGap(bs, 6.99)).not.toBeNull()
    expect(bucketForGap(bs, 7)).toBeNull()
    expect(bucketForGap(bs, 4.99)).toBeNull()
  })
  it('缺数/越界/无档位都返回 null', () => {
    expect(bucketForGap(bs, null)).toBeNull()
    expect(bucketForGap(undefined, 6)).toBeNull()
    expect(bucketForGap([], 6)).toBeNull()
  })
})

describe('chaseBadge: 只有证据够硬才提醒', () => {
  const ok = study([bucket()])
  it('stable + negative 才给徽标, 文案带证据', () => {
    const b = chaseBadge(ok, 6.2)
    expect(b?.label).toBe('追高需谨慎')
    expect(b?.note).toContain('-1.42%')
    expect(b?.note).toContain('n=320')
    expect(b?.note).toContain('前后半段同向')
    expect(b?.note).toContain('非全 A 股')          // 样本偏差必须一起说
    expect(b?.note).toContain('已剔除开盘涨停买不进的样本(412 笔)')
  })
  it('样本不足 / 前后不同向 / 结论为正 → 一律不贴', () => {
    expect(chaseBadge(study([bucket({ verdict: 'insufficient', stable: false, enough_samples: false })]), 6.2)).toBeNull()
    expect(chaseBadge(study([bucket({ verdict: 'unstable', stable: false })]), 6.2)).toBeNull()
    expect(chaseBadge(study([bucket({ verdict: 'positive', day: { ...bucket().day, mean: 1.1 } })]), 6.2)).toBeNull()
  })
  it('统计不可用 / gap 缺失 / 档位没命中 → 不贴', () => {
    expect(chaseBadge(null, 6.2)).toBeNull()
    expect(chaseBadge({ ...ok, available: false }, 6.2)).toBeNull()
    expect(chaseBadge(ok, null)).toBeNull()
    expect(chaseBadge(ok, 99)).toBeNull()
  })
  it('均值为 null 时不硬造文案', () => {
    expect(chaseBadge(study([bucket({ day: { ...bucket().day, mean: null } })]), 6.2)).toBeNull()
  })
})
