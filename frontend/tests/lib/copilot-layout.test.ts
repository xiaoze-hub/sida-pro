// @vitest-environment jsdom
/** 布局偏好与快捷键的纯逻辑：夹紧边界、持久化容错、快捷键不误触。 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  CONV_DEFAULT,
  CONV_MAX,
  CONV_MIN,
  clampConvWidth,
  parseBoolPref,
  readConvWidth,
  readPanelOpen,
  resolveShortcut,
  STORAGE_KEYS,
  writePref,
} from '@/lib/copilot/layout'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('clampConvWidth', () => {
  it('低于下限夹到下限，高于上限夹到上限', () => {
    expect(clampConvWidth(10)).toBe(CONV_MIN)
    expect(clampConvWidth(9999)).toBe(CONV_MAX)
    expect(clampConvWidth(280)).toBe(280)
  })

  it('非数/空值回默认，绝不产生 0 或 NaN 宽度', () => {
    expect(clampConvWidth(null)).toBe(CONV_DEFAULT)
    expect(clampConvWidth('abc')).toBe(CONV_DEFAULT)
    expect(clampConvWidth(Number.NaN)).toBe(CONV_DEFAULT)
    expect(clampConvWidth('')).toBe(CONV_DEFAULT)
  })

  it('字符串数字按数字处理并取整', () => {
    expect(clampConvWidth('240.6')).toBe(241)
  })
})

describe('parseBoolPref', () => {
  it('只有明确关闭值算关，缺省按开', () => {
    for (const v of ['0', 'false', 'off', ' OFF ']) expect(parseBoolPref(v)).toBe(false)
    for (const v of [null, undefined, '', '1', 'true', 'yes']) expect(parseBoolPref(v)).toBe(true)
  })
})

describe('存储读取容错', () => {
  it('storage 抛异常时回默认值，不让页面崩', () => {
    const bad = { getItem: () => { throw new Error('denied') } }
    expect(readConvWidth(bad)).toBe(CONV_DEFAULT)
    expect(readPanelOpen(bad)).toBe(true)
  })

  it('读得到就用读到的（并夹紧）', () => {
    const store = { getItem: (k: string) => (k === STORAGE_KEYS.convWidth ? '9999' : '0') }
    expect(readConvWidth(store)).toBe(CONV_MAX)
    expect(readPanelOpen(store)).toBe(false)
  })

  it('写失败不抛（隐私模式/配额满）', () => {
    const bad = { setItem: () => { throw new Error('quota') } }
    expect(() => writePref(bad, 'k', 'v')).not.toThrow()
    expect(() => writePref(null, 'k', 'v')).not.toThrow()
  })
})

describe('resolveShortcut', () => {
  const ctx = { inEditable: true, streaming: false }

  it('Cmd/Ctrl+Enter 在输入区且非流式中才是发送', () => {
    expect(resolveShortcut({ key: 'Enter', metaKey: true }, ctx)).toBe('send')
    expect(resolveShortcut({ key: 'Enter', ctrlKey: true }, ctx)).toBe('send')
    expect(resolveShortcut({ key: 'Enter', metaKey: true }, { inEditable: true, streaming: true })).toBe(null)
    expect(resolveShortcut({ key: 'Enter', metaKey: true }, { inEditable: false, streaming: false })).toBe(null)
  })

  it('输入法组合中一律不触发', () => {
    expect(resolveShortcut({ key: 'Enter', metaKey: true, isComposing: true }, ctx)).toBe(null)
  })

  it('Cmd/Ctrl+K 新会话，Cmd/Ctrl+\\ 折叠面板', () => {
    expect(resolveShortcut({ key: 'k', metaKey: true }, ctx)).toBe('new-conversation')
    expect(resolveShortcut({ key: 'K', ctrlKey: true }, ctx)).toBe('new-conversation')
    expect(resolveShortcut({ key: '\\', ctrlKey: true }, ctx)).toBe('toggle-panel')
  })

  it('无修饰键 / 带 Alt 一律不接管', () => {
    expect(resolveShortcut({ key: 'k' }, ctx)).toBe(null)
    expect(resolveShortcut({ key: 'k', metaKey: true, altKey: true }, ctx)).toBe(null)
    expect(resolveShortcut({ key: 'x', metaKey: true }, ctx)).toBe(null)
  })
})
