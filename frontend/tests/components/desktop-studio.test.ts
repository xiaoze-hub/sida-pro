import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import postcss from 'postcss'
import { contrast, resolved, tokens, type UiTheme } from '../lib/design-token-utils'

describe('Desktop workspace theme', () => {
  it('binds the desktop token scope to the authenticated workspace, not an allowlist of routes', () => {
    const app = readFileSync(resolve(__dirname, '../../src/App.tsx'), 'utf8')
    expect(app.match(/data-desktop-studio/g)).toHaveLength(1)
    expect(app).toMatch(/<RequireAuth[\s\S]*?<main[^>]*data-shell-main data-desktop-studio/)
    expect(app).not.toContain('desktopStudioPage')
  })

  it('uses the same 768px breakpoint as the desktop workspace navigation', () => {
    const css = postcss.parse(readFileSync(resolve(__dirname, '../../src/index.css'), 'utf8'))
    const studioRules: string[] = []
    css.walkRules((rule) => {
      if (!rule.selector.includes('[data-desktop-studio]')) return
      studioRules.push(rule.selector)
      expect(rule.parent?.type).toBe('atrule')
      if (rule.parent?.type === 'atrule') {
        expect(rule.parent.name).toBe('media')
        expect(rule.parent.params).toBe('(min-width: 768px)')
      }
    })
    expect(studioRules.join(' ')).toContain("data-ui='dialog'")
    expect(studioRules.join(' ')).toContain('table:has(> thead)')
    expect(studioRules.join(' ')).not.toContain("[class*='bg-']")
  })

  it('keeps the preview swatches independent from workspace surfaces', () => {
    for (const theme of ['light', 'dark', 'dim'] as const) {
      expect(tokens(theme, true)['--s1']).not.toContain('--studio-swatch-')
    }
  })

  it('does not introduce stock price colors into IC statistics', () => {
    const page = readFileSync(resolve(__dirname, '../../src/pages/FactorIC.tsx'), 'utf8')
    expect(page).not.toMatch(/text-stock-(?:up|down)\b/)
    expect(page).toContain('data-studio-ic-bar')
    expect(page).not.toMatch(/红=正相关|绿=负相关/)
  })

  it.each<UiTheme>(['light', 'dark', 'dim'])('%s workspace preserves readable surfaces and market semantics', (theme) => {
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
    for (const ink of ['--studio-ic-positive', '--studio-ic-negative', '--destructive', '--primary']) {
      expect(contrast(color(ink), color('--s1')), `${ink} on --s1`).toBeGreaterThanOrEqual(4.5)
    }
    for (const ink of ['--stock-up', '--stock-down']) {
      expect(color(ink)).toBe(resolved(current, ink))
      for (const surface of ['--s0', '--s1', '--studio-summary']) {
        expect(contrast(color(ink), color(surface)), `${ink} on ${surface}`).toBeGreaterThanOrEqual(4.5)
      }
    }
    expect(color('--chart-canvas')).toBe(resolved(current, '--chart-canvas'))
  })
})
