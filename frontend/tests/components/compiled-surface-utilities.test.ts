import { resolve } from 'node:path'
import postcss from 'postcss'
import tailwindcss from 'tailwindcss'
import loadConfig from 'tailwindcss/loadConfig'
import { describe, expect, it } from 'vitest'

// 真实编译：仅定义 CSS 变量并不保证 Tailwind 生成了对应工具类。
describe('浮层工具类真实构建', () => {
  it('生成不透明弹窗背景、成功状态色和面板阴影', async () => {
    const config = loadConfig(resolve(__dirname, '../../tailwind.config.js'))
    const result = await postcss([tailwindcss({ ...config, content: [{ raw: 'bg-popover text-popover-foreground bg-success/10 text-success shadow-panel', extension: 'html' }] })]).process('@tailwind utilities;', { from: undefined })
    const rules = new Map<string, string>()
    result.root.walkRules(rule => { rules.set(rule.selector, rule.toString()) })
    expect(rules.get('.bg-popover')).toContain('background-color: hsl(var(--popover))')
    expect(rules.get('.text-popover-foreground')).toContain('color: hsl(var(--popover-foreground))')
    expect(rules.get('.bg-success\\/10')).toContain('var(--success)')
    expect(rules.get('.text-success')).toContain('var(--success)')
    expect(rules.get('.shadow-panel')).toContain('var(--shadow-panel)')
  })
})
