/** 高开分档徽标的判定纯函数(v0.5.80 A7)。
 *
 * 判断全在后端 `src/core/gap_study.py`(样本门槛 + 前后半段一致性), 这里只做两件事:
 * ①按 gap 找档位; ②**只有 `stable && negative` 才给徽标**。
 * 样本不足(insufficient)、前后半段不同向(unstable)、甚至结论为正(positive)一律不贴 ——
 * 盘前榜单上多一个没根据的红标, 比少一个标危害大。
 */

export interface GapStat {
  n: number
  mean: number | null
  median: number | null
  win_rate: number | null
  std: number | null
  t: number | null
}

export interface GapBucket {
  bucket: string
  gap_min: number
  gap_max: number
  day: GapStat
  next: GapStat
  halves: { first: GapStat; second: GapStat }
  enough_samples: boolean
  stable: boolean
  verdict: 'insufficient' | 'unstable' | 'negative' | 'positive' | string
}

export interface GapStudyResp {
  available: boolean
  reason?: string | null
  window_days: number
  since?: string
  latest_date?: string | null
  universe: { symbols: number; observations: number; note?: string }
  excluded_limit_up?: number
  min_samples?: number
  buckets: GapBucket[]
}

export function bucketForGap(buckets: GapBucket[] | undefined, gapPct: number | null): GapBucket | null {
  if (!buckets?.length || gapPct == null || !Number.isFinite(gapPct)) return null
  return buckets.find((b) => gapPct >= b.gap_min && gapPct < b.gap_max) ?? null
}

export interface ChaseBadge {
  label: string
  note: string
}

/** 该 gap 档位是否值得提醒"追高需谨慎"。 */
export function chaseBadge(study: GapStudyResp | null, gapPct: number | null): ChaseBadge | null {
  if (!study?.available) return null
  const b = bucketForGap(study.buckets, gapPct)
  if (!b || !b.stable || b.verdict !== 'negative' || b.day.mean == null) return null
  const win = b.day.win_rate == null ? '' : `, 胜率 ${Math.round(b.day.win_rate * 100)}%`
  const nxt = b.next.mean == null ? '' : `; 拿到次日 ${b.next.mean > 0 ? '+' : ''}${b.next.mean}%`
  return {
    label: '追高需谨慎',
    note: `近 ${study.window_days} 日「${b.bucket}」(高开 ${b.gap_min}%~${b.gap_max}%) 开盘买入`
      + `当日平均 ${b.day.mean}%${win}${nxt}; n=${b.day.n}, 前后半段同向`
      + `; 已剔除开盘涨停买不进的样本${study.excluded_limit_up ? `(${study.excluded_limit_up} 笔)` : ''}`
      + `。${study.universe.note ?? ''}`,
  }
}
