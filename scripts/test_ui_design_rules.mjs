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

test('开关使用共享组件，拒绝手写 role switch 轨道', () => {
  assert.equal(auditPresentation('<Switch checked={enabled} />').length, 0)
  assert.equal(auditPresentation('<button role="switch"><span /></button>')[0].rule, 'R21-SHARED-SWITCH')
})

test('内容卡片拒绝边框、hover 描边和装饰 ring', () => {
  for (const code of ['<Card className="border-l-2 border-primary" />', '<div className="analysis-section hover:border-primary ring-1" />', '<section className="rounded-xl border border-border bg-card" />']) {
    assert.ok(auditPresentation(code).some(item => item.rule === 'R22-BORDERLESS-CARD'))
  }
})
test('无边框卡片保留键盘焦点，表单、表格、页签栏和浮层可有用途明确的边线', () => {
  for (const code of ['<Card className="border-none focus-visible:ring-2" />', '<Input className="border border-input bg-card" />', '<tr className="border-b border-border" />', '<div role="tablist" className="rounded-lg border border-border bg-s2" />', '<div className="absolute rounded-xl border border-border bg-card shadow-float" />']) {
    assert.equal(auditPresentation(code).filter(item => item.rule === 'R22-BORDERLESS-CARD').length, 0)
  }
})
test('拒绝恢复卡片内侧高光或取消全局无边框约束', () => {
  const css = ':root { --background: var(--s0); --card: var(--s1); --popover: var(--s4); }'
  assert.ok(auditThemeAliases(css + ':root { --panel-highlight: inset 0 1px white; }').length)
  assert.ok(auditThemeAliases(css + '.card { border: 1px solid; }').length)
  assert.deepEqual(auditThemeAliases(css + '.card-surface, .card { border: none !important; }'), [])
})
