// @vitest-environment jsdom
//
// UI 走查 P0 修复的回归(2026-09-18):
//  ① 「固定免责条遮挡内容」—— Disclaimer 显示时必须给 <html> 打 `has-disclaimer`(内容区据此
//     用 `--disclaimer-h` 补偿 padding-bottom), 关闭/卸载时必须摘掉(别留永久空隙);
//  ② 「图表库不再依赖外部 CDN」—— 源码层面: 两个图表组件都 import 打包版 lightweight-charts,
//     且 index.html 里没有 unpkg/jsdelivr(CI 的 R9 门禁只查 html, 这里补 .tsx 侧)。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import Disclaimer from '@/components/Disclaimer'

afterEach(() => {
  cleanup()
  document.documentElement.classList.remove('has-disclaimer')
  try {
    localStorage.clear()
  } catch {
    /* ignore */
  }
})

beforeEach(() => {
  document.documentElement.classList.remove('has-disclaimer')
  try {
    localStorage.clear()
  } catch {
    /* ignore */
  }
})

describe('免责条不再遮挡内容(P0-1, 2026-10-24 改文档流页脚)', () => {
  it('渲染免责条但不给 <html> 打 has-disclaimer(不再需要底部补偿)', () => {
    render(<Disclaimer />)
    expect(screen.getByText(/不构成投资建议/)).toBeTruthy()
    // L3-G: 从 fixed 底栏改为随内容滚动的页脚 —— 既然不压内容, 就不该再留补偿 padding
    expect(document.documentElement.classList.contains('has-disclaimer')).toBe(false)
  })

  it('样式上不再是 fixed 覆盖层(源码级守卫, 防止改回压住最后一行)', () => {
    const src = readFileSync(resolve(__dirname, '../../src/components/Disclaimer.tsx'), 'utf-8')
    const rootClass = src.match(/className="([^"]*\bborder-t\b[^"]*)"/)
    expect(rootClass).toBeTruthy()
    expect(rootClass![1]).not.toMatch(/\bfixed\b|\bsticky\b/)
  })

  it('点关闭后不显示且不留标记', () => {
    render(<Disclaimer />)
    fireEvent.click(screen.getByTitle('关闭并不再显示'))
    expect(document.documentElement.classList.contains('has-disclaimer')).toBe(false)
  })

  it('已关闭过的用户: 不渲染且不打标记', () => {
    localStorage.setItem('sida_disclaimer_dismissed', '1')
    render(<Disclaimer />)
    expect(screen.queryByText(/不构成投资建议/)).toBeNull()
    expect(document.documentElement.classList.contains('has-disclaimer')).toBe(false)
  })
})

describe('CSS 变量确实定义了补偿高度(P0-1)', () => {
  it('index.css 定义 --disclaimer-h 且分端', () => {
    const css = readFileSync(resolve(__dirname, '../../src/index.css'), 'utf-8')
    expect(css).toContain('--disclaimer-h')
    expect(css).toContain('html.has-disclaimer')
    expect(css).toMatch(/max-width:\s*767px/)
  })

  it('App 外壳用 var(--disclaimer-h) 补偿底部', () => {
    const app = readFileSync(resolve(__dirname, '../../src/App.tsx'), 'utf-8')
    expect(app).toContain('--disclaimer-h')
  })
})

describe('图表库自托管, 不依赖 CDN(P0-2)', () => {
  const biz = resolve(__dirname, '../../packages/biz-ui/src/components')

  it('KlineChart / MinuteLwcChart 都 import 了打包版 lightweight-charts', () => {
    // P3(2026-09-18): InteractiveKline 已按评估退役, 断言对象改为存活的两个组件
    for (const f of ['KlineChart.tsx', 'MinuteLwcChart.tsx']) {
      const src = readFileSync(resolve(biz, f), 'utf-8')
      // 打包版图表库: 有的组件用命名导入, 有的用命名空间导入 —— 都算"来自 npm 包"
      expect(src, `${f} 应从 lightweight-charts 包导入`).toMatch(
        /import [^;]*from ['"]lightweight-charts['"]/,
      )
      // 不许再靠 CDN 全局(window.LightweightCharts)取库
      expect(src, `${f} 不应依赖 CDN 全局`).not.toMatch(/window\.LightweightCharts/)
      expect(src, `${f} 不应出现 CDN 域名`).not.toMatch(/unpkg\.com|cdn\.jsdelivr\.net/)
    }
  })

  it('index.html 不含外部 CDN 脚本', () => {
    const html = readFileSync(resolve(__dirname, '../../index.html'), 'utf-8')
    expect(html).not.toMatch(/unpkg\.com/)
    expect(html).not.toMatch(/cdn\.jsdelivr\.net/)
  })
})
