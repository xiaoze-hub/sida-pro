// P0(2026-10-18 审计 F19/R19): 内部符号前缀不得出现在页面上(USZA/USHT …)。
import { describe, expect, it } from 'vitest'
import { hasInternalSymbolPrefix, normSymbol, stripInternalPrefix } from '@/lib/symbol'

describe('normSymbol', () => {
  it('剥 thsdk 内部前缀(沪/深/北各一例)', () => {
    expect(normSymbol('USZA000006')).toBe('000006')
    expect(normSymbol('USHT603922')).toBe('603922')
    expect(normSymbol('USHA600000')).toBe('600000')
    expect(normSymbol('USBJ920001')).toBe('920001')
  })
  it('剥交易所后缀 / 腾讯前缀', () => {
    expect(normSymbol('002361.SZ')).toBe('002361')
    expect(normSymbol('600000.SH')).toBe('600000')
    expect(normSymbol('sh600000')).toBe('600000')
  })
  it('本来就是 6 位则原样返回', () => {
    expect(normSymbol('002361')).toBe('002361')
  })
  it('空值不抛异常', () => {
    expect(normSymbol(null)).toBe('')
    expect(normSymbol(undefined)).toBe('')
  })
})

describe('stripInternalPrefix(路由参数用)', () => {
  it('只剥 USxx 前缀, 不动板块代码后缀', () => {
    expect(stripInternalPrefix('USHT603922')).toBe('603922')
    // 板块代码 881101.SH 走同一路由时不能被改成 881101
    expect(stripInternalPrefix('881101.SH')).toBe('881101.SH')
  })
})

describe('hasInternalSymbolPrefix', () => {
  it('识别残留前缀', () => {
    expect(hasInternalSymbolPrefix('代码 USZA000006')).toBe(true)
    expect(hasInternalSymbolPrefix('000006')).toBe(false)
  })
})
