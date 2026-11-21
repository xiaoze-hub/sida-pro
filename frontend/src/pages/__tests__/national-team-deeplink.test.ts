// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'

import { parseDeepLink } from '../NationalTeam'

/**
 * 参谋直查深链（2026-11-21，首批 C 页面对拍）。
 *
 * 直查回答末尾给出的链接形如
 * `/national-team?kind=float&report_date=2026-09-30&holder=huijin&view=holdings`
 * —— 页面必须按同一口径还原，不能把「汇金·某报告期·十大流通股东」落到
 * 「全部机构·最新一期·十大股东」上去（那会让用户复核不到同一个数）。
 *
 * 这里只测纯解析：非法值必须回落缺省，不允许把任意字符串透传给后端。
 */

describe('parseDeepLink: 直查链接 → 页面状态', () => {
  it('完整链接逐项还原（含机构、报告期、榜单口径、变动过滤、个股）', () => {
    const got = parseDeepLink(
      new URLSearchParams(
        'kind=total&report_date=2026-09-30&holder=ssb&change=new,increase&view=changes&q=601398',
      ),
    )
    expect(got).toEqual({
      kind: 'total',
      reportDate: '2026-09-30',
      holder: 'ssb',
      change: ['new', 'increase'],
      view: 'changes',
      q: '601398',
    })
  })

  it('空链接回落页面缺省：float / 最新一期 / 全部机构 / 无变动过滤', () => {
    expect(parseDeepLink(new URLSearchParams(''))).toEqual({
      kind: 'float',
      reportDate: null,
      holder: 'all',
      change: [],
      view: null,
      q: '',
    })
  })

  it('非法 kind / view / 日期 / 变动值一律回落，不猜', () => {
    const got = parseDeepLink(
      new URLSearchParams(
        'kind=quarterly&report_date=2026-09&holder=&change=新进,new,bogus&view=detail',
      ),
    )
    expect(got.kind).toBe('float')
    expect(got.reportDate).toBeNull()
    expect(got.holder).toBe('all')
    expect(got.change).toEqual(['new'])
    expect(got.view).toBeNull()
  })

  it('只有 view=holdings 也保留（持仓明细定位）', () => {
    expect(parseDeepLink(new URLSearchParams('view=holdings')).view).toBe('holdings')
  })
})
