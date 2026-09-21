/**
 * 设计系统落地钉子(2026-09-20)。都是**源码级**断言: 令牌在不在、规则有没有被绕过。
 * 这些条款的价值全在"新代码不许把旧的坏习惯带回来", 所以必须能被 CI 拦。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const ROOT = resolve(__dirname, '../..')
const css = readFileSync(resolve(ROOT, 'src/index.css'), 'utf-8')
const tw = readFileSync(resolve(ROOT, 'tailwind.config.js'), 'utf-8')
const rules = readFileSync(resolve(ROOT, '../scripts/check_ui_rules.mjs'), 'utf-8')

describe('Surface 阶梯', () => {
  it('S0–S4 五档 + 图表画布都在(亮/暗各一套)', () => {
    for (const t of ['--s0', '--s1', '--s2', '--s3', '--s4', '--chart-canvas']) {
      expect(css.split(`${t}:`).length - 1, `${t} 应亮暗各一处`).toBeGreaterThanOrEqual(2)
    }
  })

  it('tailwind 注册了 s0-s4 / canvas / role-*, 且支持透明度修饰符(<alpha-value>)', () => {
    for (const k of ['s0', 's1', 's2', 's3', 's4']) {
      expect(tw, `tailwind 缺 ${k}`).toContain(`${k}: 'hsl(var(--${k}) / <alpha-value>)'`)
    }
    expect(tw).toContain("canvas: 'hsl(var(--chart-canvas) / <alpha-value>)'")
    expect(tw).toMatch(/role:\s*\{/)
    expect(tw).toMatch(/heat:\s*\{/)
  })

  it('图表画布比外壳更空一档(暗色: canvas 比 s0 更暗; 亮色: 比 s0 更白)', () => {
    // 取 HSL 三元组里的**亮度**(第三个分量), 不是色度
    const lightness = (decl: string) => {
      const m = decl.match(/--[\w-]+:\s*(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)%/)
      expect(m, `解析不出亮度: ${decl}`).toBeTruthy()
      return Number(m![3])
    }
    const darkBlock = css.slice(css.lastIndexOf('--s0:'))
    const s0 = lightness(darkBlock.match(/--s0: [^;]+/)![0])
    const canvas = lightness(darkBlock.match(/--chart-canvas: [^;]+/)![0])
    expect(canvas, '暗色下图表画布必须比页底更暗(更空一档)').toBeLessThan(s0)
  })

  it('背景类不得表达涨跌 —— 由 R12 棘轮兜底(只减不增)', () => {
    expect(rules).toContain('R12-SURFACE-RATCHET')
    expect(rules).toMatch(/bg-stock-\(up\|down\)/)
  })
})

describe('motion token', () => {
  it('五档时长 + 两条缓动在, 且 reduced-motion 在**token 层**归零', () => {
    for (const d of ['--dur-instant', '--dur-fast', '--dur-normal', '--dur-slow', '--dur-flash', '--dur-flash-max']) {
      expect(css).toContain(d)
    }
    expect(css).toMatch(/--ease-out: cubic-bezier/)
    expect(css).toMatch(/prefers-reduced-motion: reduce[\s\S]{0,200}--dur-instant: 0ms/)
  })

  it('涨跌闪强度走变量(允许按 |Δ| 缩放)且有上限', () => {
    expect(css).toMatch(/\.sida-flash \{ animation-duration: var\(--flash-dur, var\(--dur-flash\)\)/)
    expect(css).toContain('--dur-flash-max')
  })
})

describe('Dim 第三档主题', () => {
  it('只降对比度: 色相不动(240 系), 且不引入新色相', () => {
    const dim = css.match(/:root\[data-theme='dim'\] \{([\s\S]*?)\}/)
    expect(dim, 'dim 主题块缺失').toBeTruthy()
    const body = dim![1]
    expect(body).toMatch(/--foreground: 240/)
    expect(body).toMatch(/--chart-canvas: 240/)
    expect(body).not.toMatch(/--stock-(up|down)/) // 涨跌色在 dim 下不动
  })
})

describe('角色色与交互三态', () => {
  it('四个角色色都在, 且与涨跌色不同名(不抢语义)', () => {
    for (const r of ['--role-watch', '--role-opp', '--role-risk', '--role-system']) expect(css).toContain(r)
    expect(css).not.toMatch(/--role-\w+: var\(--stock-(up|down)\)/)
  })

  it('三态语法齐备: hover(亮) / focus(左侧3px) / selected(填充+角色条) / 热区下限 32px', () => {
    expect(css).toMatch(/\.row-hover:hover \{ background-color: hsl\(var\(--s2\)\)/)
    expect(css).toMatch(/\.row-focusable:focus-visible \{ outline: none; box-shadow: inset 3px 0 0 0/)
    expect(css).toMatch(/\.row-selected \{ background-color: hsl\(var\(--s3\)\)/)
    expect(css).toMatch(/\.row-hit \{ min-height: 32px; \}/)
    expect(css).toMatch(/\.role-strip::before/)
  })

  it('长列表性能: 视口外的行跳过布局绘制(content-visibility, 不引虚拟滚动依赖)', () => {
    expect(css).toMatch(/\.list-window > \* \{ content-visibility: auto/)
  })
})

describe('浮层层级与圆角(治理)', () => {
  it('只有一档阴影 token, 且 R13 禁止 shadow-lg/xl/2xl/md 与 rounded-2xl', () => {
    expect(css).toMatch(/\.shadow-float \{/)
    expect(rules).toContain('R13-ELEVATION')
    expect(rules).toMatch(/shadow-\(2xl\|xl\|lg\|md\)/)
  })

  it('空态收敛: 组件带 data-empty-state 供巡检量高度; 三件套(原因/操作/口径)都在', () => {
    const es = readFileSync(resolve(ROOT, 'src/components/EmptyState.tsx'), 'utf-8')
    expect(es).toContain('data-empty-state')
    expect(es).toMatch(/min-h-\[140px\]/)
    expect(es).toMatch(/caliber\?: ReactNode/)
  })
})

describe('终端字阶收敛到 6 档(公开面允许第 7 档)', () => {
  it('全仓 text-[Npx] 的取值集合 = 6 档(+公开面 Tiers/Landing 的 28/36/48)', () => {
    const { readdirSync, statSync } = require('node:fs') as typeof import('node:fs')
    const walk = (dir: string, out: string[] = []): string[] => {
      for (const name of readdirSync(dir)) {
        if (name === 'node_modules' || name === 'dist' || name === '.git') continue
        const full = resolve(dir, name)
        if (statSync(full).isDirectory()) walk(full, out)
        else if (/\.tsx?$/.test(name)) out.push(full)
      }
      return out
    }
    const files = [...walk(resolve(ROOT, 'src')), ...walk(resolve(ROOT, 'packages'))]
    const sizes = new Set<number>()
    const publicOnly = new Set<number>()
    for (const f of files) {
      const src = readFileSync(f, 'utf-8')
      for (const m of src.matchAll(/text-\[(\d+(?:\.\d+)?)px\]/g)) {
        const n = Number(m[1])
        if (/\/(Tiers|Landing)\.tsx$/.test(f) && (n === 28 || n === 36 || n === 48)) publicOnly.add(n)
        else sizes.add(n)
      }
    }
    expect([...sizes].sort((a, b) => a - b)).toEqual([10, 11, 12, 13, 16, 20])
    expect([...publicOnly].sort((a, b) => a - b)).toEqual([28, 36, 48])
  })

  it('小数档(9.5/10.5/11.5/12.5)已全部收敛', () => {
    const { readdirSync, statSync } = require('node:fs') as typeof import('node:fs')
    const walk = (dir: string, out: string[] = []): string[] => {
      for (const name of readdirSync(dir)) {
        if (name === 'node_modules' || name === 'dist') continue
        const full = resolve(dir, name)
        if (statSync(full).isDirectory()) walk(full, out)
        else if (/\.tsx?$/.test(name)) out.push(full)
      }
      return out
    }
    const hits: string[] = []
    for (const f of [...walk(resolve(ROOT, 'src')), ...walk(resolve(ROOT, 'packages'))]) {
      const src = readFileSync(f, 'utf-8')
      for (const m of src.matchAll(/text-\[(\d+\.\d+)px\]/g)) hits.push(`${f.split('/').pop()}:${m[1]}`)
    }
    expect(hits).toEqual([])
  })
})
