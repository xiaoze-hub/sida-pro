import { describe, expect, it } from 'vitest'
import { contrast, resolved, tokens, type UiTheme } from '../lib/design-token-utils'

describe.each<UiTheme>(['light', 'dark', 'dim'])('%s 主题可读性与边界', (theme) => {
  const values = tokens(theme)
  const color = (name: string) => resolved(values, name)

  it.each(['--s0', '--s1', '--s2', '--s3', '--s4'])('正文与辅助文字在 %s 上保持 AA 对比度', (surface) => {
    expect(contrast(color('--foreground'), color(surface))).toBeGreaterThanOrEqual(4.5)
    expect(contrast(color('--muted-foreground'), color(surface))).toBeGreaterThanOrEqual(4.5)
  })
  it('主按钮、涨跌文字在实际背景中可读', () => {
    expect(contrast(color('--primary'), color('--primary-foreground'))).toBeGreaterThanOrEqual(4.5)
    for (const ink of ['--stock-up', '--stock-down']) {
      for (const surface of ['--s0', '--s1']) {
        expect(contrast(color(ink), color(surface)), `${ink} on ${surface}`).toBeGreaterThanOrEqual(4.5)
      }
    }
  })
  it('结构边界可见，输入框边界至少 3:1', () => {
    expect(contrast(color('--border-strong'), color('--s1'))).toBeGreaterThanOrEqual(1.5)
    expect(contrast(color('--input'), color('--s1'))).toBeGreaterThanOrEqual(3)
    if (theme !== 'light') expect(contrast(color('--s0'), color('--s1'))).toBeGreaterThanOrEqual(1.15)
  })
  it('主题别名保持同一空间来源', () => {
    expect(color('--card')).toBe(color('--s1'))
    expect(color('--background')).toBe(color('--s0'))
    expect(color('--popover')).toBe(color('--s4'))
  })
})
