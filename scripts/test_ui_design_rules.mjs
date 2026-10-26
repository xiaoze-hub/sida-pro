import assert from 'node:assert/strict'
import test from 'node:test'
import { auditPresentation, auditThemeAliases } from './lib/ui-design-rules.mjs'

test('拒绝变体、任意值和数字形式的透明面板', () => {
  const bad = auditPresentation('dark:bg-card/40 hover:bg-s1/[0.6] bg-popover/95 bg-background/50')
  assert.equal(bad.filter((item) => item.rule === 'R17-OPAQUE-SURFACE').length, 4)
  assert.equal(auditPresentation('bg-card hover:bg-s2 text-[14px] border-border').length, 0)
})
test('保留行情/状态淡底，不误判辅助状态色', () => {
  assert.equal(auditPresentation('bg-primary/10 bg-amber-500/10 text-stock-up text-[12px]').length, 0)
})
test('小字与低透明结构边界给出准确行号', () => {
  const bad = auditPresentation('text-[14px]\ntext-[11px] border-border/40')
  assert.deepEqual(bad.map(({ rule, line }) => [rule, line]), [['R18-READABLE-TYPE', 2], ['R19-STRUCTURAL-BORDER', 2]])
})
test('别名保持单一来源，拒绝主题内重复值与全局 Dim', () => {
  const css = ':root { --background: var(--s0); --card: var(--s1); --popover: var(--s4); }'
  assert.deepEqual(auditThemeAliases(css), [])
  assert.equal(auditThemeAliases(css + '.dark { --card: 220 20% 10%; }').length, 1)
  assert.equal(auditThemeAliases(css + ':root[data-theme="dim"] {}').length, 1)
})
