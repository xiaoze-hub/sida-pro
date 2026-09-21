/**
 * 新鲜度与会话态(2026-09-20 设计系统 P0)。
 * 判据(诚实口径): **时间未知不能说实时**; 收盘后不许把静态数据标成 live; 年龄没有就是 `--` 不是 0。
 */
import { describe, expect, it } from 'vitest'
import { formatAge, freshnessOf } from '@panwatch/biz-ui/components/FreshnessBadge'
import { sessionPhaseOf } from '@panwatch/biz-ui/components/SessionPhaseChip'

const NOW = Date.parse('2026-09-20T06:00:00Z') // 北京时间 14:00(交易时段内)
const iso = (secAgo: number) => new Date(NOW - secAgo * 1000).toISOString()

describe('freshnessOf', () => {
  it('窗口内 → live; 超实时窗口 → delay; 超可用窗口 → stale', () => {
    expect(freshnessOf(iso(10), { now: NOW }).level).toBe('live')
    expect(freshnessOf(iso(600), { now: NOW }).level).toBe('delay')
    expect(freshnessOf(iso(3600), { now: NOW }).level).toBe('stale')
  })

  it('边界: 恰好等于窗口值仍算窗口内(不因 1ms 抖动翻脸)', () => {
    expect(freshnessOf(iso(90), { now: NOW }).level).toBe('live')
    expect(freshnessOf(iso(600), { now: NOW }).level).toBe('delay')
  })

  it('**时间未知 → stale, 不是 live**(不知道时间就不能说它实时)', () => {
    const r = freshnessOf(null, { now: NOW })
    expect(r.level).toBe('stale')
    expect(r.ageSec).toBeNull()
    expect(r.label).toBe('时间未知')
    expect(freshnessOf(undefined, { now: NOW }).level).toBe('stale')
    expect(freshnessOf('', { now: NOW }).level).toBe('stale')
    expect(freshnessOf('not-a-date', { now: NOW }).level).toBe('stale')
  })

  it('收盘态一律 closed(与数据年龄无关: 收盘后没有"实时"这回事)', () => {
    expect(freshnessOf(iso(1), { now: NOW, marketClosed: true }).level).toBe('closed')
    expect(freshnessOf(null, { now: NOW, marketClosed: true }).level).toBe('closed')
  })

  it('未来时间戳不产出负年龄(时钟偏差不许显示成 "-3s")', () => {
    expect(freshnessOf(new Date(NOW + 5000), { now: NOW }).ageSec).toBe(0)
  })
})

describe('formatAge', () => {
  it('秒/分/时; 未知显示 -- 而非 0', () => {
    expect(formatAge(5)).toBe('5s')
    expect(formatAge(125)).toBe('2m')
    expect(formatAge(7200)).toBe('2h')
    expect(formatAge(null)).toBe('--')
  })
})

describe('sessionPhaseOf(按北京时间, 与浏览器时区无关)', () => {
  const at = (h: number, m: number, day = 21) => new Date(Date.UTC(2026, 8, day, h - 8, m)) // day=21 是周一

  it('五个时段各归其位', () => {
    expect(sessionPhaseOf(at(8, 30)).phase).toBe('pre')
    expect(sessionPhaseOf(at(9, 20)).phase).toBe('auction')
    expect(sessionPhaseOf(at(10, 0)).phase).toBe('morning')
    expect(sessionPhaseOf(at(12, 0)).phase).toBe('noon')
    expect(sessionPhaseOf(at(14, 0)).phase).toBe('afternoon')
    expect(sessionPhaseOf(at(15, 30)).phase).toBe('closed')
  })

  it('只有连续竞价/集合竞价在"跳动"(ticking)', () => {
    expect(sessionPhaseOf(at(10, 0)).ticking).toBe(true)
    expect(sessionPhaseOf(at(9, 20)).ticking).toBe(true)
    expect(sessionPhaseOf(at(12, 0)).ticking).toBe(false)
    expect(sessionPhaseOf(at(16, 0)).ticking).toBe(false)
  })

  it('周末休市(周六/周日都不算交易时段)', () => {
    expect(sessionPhaseOf(at(10, 0, 26)).phase).toBe('weekend') // 周六
    expect(sessionPhaseOf(at(10, 0, 27)).phase).toBe('weekend') // 周日
  })

  it('与运行环境时区无关(UTC 时刻换算成北京时间后判定)', () => {
    // UTC 02:00 = 北京 10:00 → 上午连续竞价
    expect(sessionPhaseOf(new Date(Date.UTC(2026, 8, 21, 2, 0))).phase).toBe('morning')
  })
})
