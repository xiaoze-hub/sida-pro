import { describe, expect, it } from 'vitest'
import { desktopStudioPage } from '@/lib/desktop-studio'
import { contrast, resolved, tokens, type UiTheme } from '../lib/design-token-utils'

describe('Desktop Studio preview', () => {
  it.each([
    ['/', 'home'], ['/stocks/600519', 'quote'], ['/stocks/000001/', 'quote'],
    ['/settings', 'settings'], ['/settings/', 'settings'],
    ['/stocks', undefined], ['/quotes', undefined], ['/login', undefined],
    ['/reports', undefined], ['/stocks/600519/reports', undefined], ['/settings-extra', undefined],
  ] as const)('limits the preview on %s to %s', (path, page) => {
    expect(desktopStudioPage(path)).toBe(page)
  })

  it.each<UiTheme>(['light', 'dark', 'dim'])('%s preview preserves readable surfaces and market semantics', (theme) => {
    const preview = tokens(theme, true)
    const current = tokens(theme)
    const color = (key: string) => resolved(preview, key)
    for (const surface of ['--s0', '--s1', '--s2', '--s3', '--s4', '--studio-rail', '--studio-summary']) {
      for (const ink of ['--foreground', '--muted-foreground']) {
        expect(contrast(color(ink), color(surface)), `${ink} on ${surface}`).toBeGreaterThanOrEqual(4.5)
      }
    }
    expect(contrast(color('--accent-foreground'), color('--s3'))).toBeGreaterThanOrEqual(4.5)
    expect(contrast(color('--primary'), color('--primary-foreground'))).toBeGreaterThanOrEqual(4.5)
    expect(contrast(color('--input-focus'), color('--s1'))).toBeGreaterThanOrEqual(3)
    for (const ink of ['--stock-up', '--stock-down']) {
      expect(color(ink)).toBe(resolved(current, ink))
      for (const surface of ['--s0', '--s1', '--studio-summary']) {
        expect(contrast(color(ink), color(surface)), `${ink} on ${surface}`).toBeGreaterThanOrEqual(4.5)
      }
    }
    expect(color('--chart-canvas')).toBe(resolved(current, '--chart-canvas'))
  })
})
