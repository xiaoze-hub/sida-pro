/**
 * 主线数据时点/缓存时效文案契约(2026-11-09 复审修复)
 *
 * 复审结论: 行业视图与空态只显示「HH:mm:ss 墙钟」——那是**本次拉取**时间,
 * 既不是涨停池数据日, 也不带缓存时效; 后端明明回了 data_meta.as_of / stale / age_s,
 * 前端却一个没消费。用户于是无法判断「这张榜是哪天的」「是不是旧快照」。
 *
 * 这里锁死纯函数口径:
 *  ① as_of 缺失 → 明写"时点未知", 绝不拿今天/墙钟冒充;
 *  ② stale=true → 必须标旧数据; age_s 有值给人读的年龄, 无值写"时效未知";
 *  ③ stale 缺失 → 同样写"时效未知"(契约缺失 ≠ 新鲜);
 *  ④ stale=false + age_s → 展示数据年龄, 不写死"实时"。
 */
import { describe, expect, it } from 'vitest'

import {
  mainlineAsOfDate,
  mainlineAsOfText,
  mainlineCacheText,
  mainlineSourceLine,
} from '@sida/biz-ui/lib/mainline-meta'

describe('mainlineAsOfDate', () => {
  it('接受 ISO 日期与带时间戳的 ISO, 只取日期段', () => {
    expect(mainlineAsOfDate('2026-11-06')).toBe('2026-11-06')
    expect(mainlineAsOfDate('2026-11-06T09:31:05+08:00')).toBe('2026-11-06')
  })

  it('空值/非 ISO 一律 null(不猜成今天)', () => {
    expect(mainlineAsOfDate(null)).toBeNull()
    expect(mainlineAsOfDate('')).toBeNull()
    expect(mainlineAsOfDate('11-06')).toBeNull()
    expect(mainlineAsOfDate('2026/11/06')).toBeNull()
    expect(mainlineAsOfDate('not-a-date')).toBeNull()
  })
})

describe('mainlineAsOfText', () => {
  it('有数据日 → MM-DD', () => {
    expect(mainlineAsOfText('2026-11-06')).toBe('11-06')
  })

  it('无数据日 → 明写时点未知', () => {
    expect(mainlineAsOfText(null)).toBe('时点未知')
  })
})

describe('mainlineCacheText', () => {
  it('stale=true + age_s → 旧数据 + 人读年龄', () => {
    expect(mainlineCacheText(true, 742.4)).toContain('旧数据')
    expect(mainlineCacheText(true, 742.4)).toContain('12 分钟前')
  })

  it('stale=true + age_s 缺失 → 旧数据 + 时效未知', () => {
    const text = mainlineCacheText(true, null)
    expect(text).toContain('旧数据')
    expect(text).toContain('时效未知')
  })

  it('stale 缺失(契约没给) → 时效未知, 不假装新鲜', () => {
    expect(mainlineCacheText(null, null)).toContain('时效未知')
    expect(mainlineCacheText(undefined, 30)).toContain('时效未知')
  })

  it('stale=false + age_s → 缓存年龄(秒级)', () => {
    expect(mainlineCacheText(false, 42.6)).toContain('43 秒前')
  })

  it('stale=false + age_s 缺失 → null(无依据就不写缓存结论)', () => {
    expect(mainlineCacheText(false, null)).toBeNull()
  })
})

describe('mainlineSourceLine (首页结论来源标签)', () => {
  it('后端给了 data_meta.as_of 就显示数据日, 不再硬编码"日期未获取"', () => {
    const line = mainlineSourceLine({ asOf: '2026-11-06', stale: false, ageS: 42.6 })
    expect(line).toContain('数据日 11-06')
    expect(line).not.toContain('日期未获取')
    expect(line).toContain('缓存 43 秒前')
  })

  it('只有后端确实没给日期时才写"日期未获取"', () => {
    expect(mainlineSourceLine({ asOf: null, stale: null, ageS: null })).toContain('日期未获取')
  })

  it('首拉中不冒充"未知"', () => {
    expect(mainlineSourceLine({ asOf: null, loading: true })).toBe('数据日获取中')
  })

  it('旧数据也随标签一起暴露', () => {
    const line = mainlineSourceLine({ asOf: '2026-11-06', stale: true, ageS: 720 })
    expect(line).toContain('数据日 11-06')
    expect(line).toContain('旧数据')
    expect(line).toContain('12 分钟前')
  })
})
