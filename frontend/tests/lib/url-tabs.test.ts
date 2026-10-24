// tabFromSearch: /opportunities?tab= 深层链接解析(纯函数)
import { describe, expect, it } from 'vitest'
import { tabFromSearch } from '@/lib/url-tabs'

const ALLOWED = ['resonance', 'strategy', 'tdx', 'wencai', 'stockpool', 'jbp', 'cb'] as const

describe('tabFromSearch', () => {
  it('标准 ?tab= 解析', () => {
    expect(tabFromSearch('?tab=resonance', ALLOWED)).toBe('resonance')
    expect(tabFromSearch('?tab=stockpool', ALLOWED)).toBe('stockpool')
  })

  it('容忍无问号与多余参数', () => {
    expect(tabFromSearch('tab=resonance', ALLOWED)).toBe('resonance')
    expect(tabFromSearch('?foo=1&tab=resonance&x=2', ALLOWED)).toBe('resonance')
  })

  it('非法/缺失/不在白名单 → null(调用方保持默认)', () => {
    expect(tabFromSearch('', ALLOWED)).toBeNull()
    expect(tabFromSearch('?', ALLOWED)).toBeNull()
    expect(tabFromSearch('?foo=1', ALLOWED)).toBeNull()
    expect(tabFromSearch('?tab=', ALLOWED)).toBeNull()
    // 白名单外的一律拒绝 —— 不允许 ?tab= 随意改内部状态
    expect(tabFromSearch('?tab=admin', ALLOWED)).toBeNull()
    expect(tabFromSearch('?tab=RESonance', ALLOWED)).toBeNull()
  })
})
