/**
 * P0-1(2026-09-18) 钉子: 行情终端页"把宽度还给 K 线"的三处改动不许回退。
 *
 * 为什么用源码断言而不是渲染断言: `StockWorkbench` 依赖 api/router/insights 一大串上下文,
 * jsdom 里渲染它得 mock 十几个模块, 脆且慢; 而这三条**本质上都是"代码里有没有这个判定"**,
 * 源码断言既准确又不会因无关重构误红(与本仓既有 `kline-minute-mode` 的做法一致)。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../..')
const read = (p: string) => readFileSync(resolve(root, p), 'utf-8')

const workbench = read('src/pages/StockWorkbench.tsx')
const app = read('src/App.tsx')

describe('P0-1 个股工作台: 右栏默认折叠 + 宽度全部给 K 线', () => {
  it('右栏有折叠开关, 且默认值是"折叠"(只有显式存过 1 才展开)', () => {
    expect(workbench).toContain('rail-toggle')
    expect(workbench).toMatch(/localStorage\.getItem\('sida_workbench_rail'\) === '1'/)
  })

  it('用户点过之后会被记住(写回 localStorage), 不跟用户较劲', () => {
    expect(workbench).toMatch(/localStorage\.setItem\('sida_workbench_rail'/)
  })

  it('折叠态保留"展开"入口 —— 折叠只改展示, 不删功能', () => {
    expect(workbench).toContain('展开速览栏')
    expect(workbench).toContain('收起速览栏')
  })

  it('展开态仍然渲染速览栏与区间统计(功能未删)', () => {
    expect(workbench).toMatch(/<QuickRail symbol=\{symbol\} market=\{MARKET\} \/>/)
    expect(workbench).toMatch(/RangeStatsCard stats=\{rangeStats\}/)
  })

  it('容器放开 1500px 上限(工作台独占整宽)', () => {
    expect(workbench).toContain('mx-auto max-w-none p-3')
    expect(workbench).not.toContain('max-w-[1500px]')
  })
})

describe('P0-1 外壳: 工作台页默认折叠侧栏', () => {
  it('没表过态的用户在 /stocks/* 默认折叠(显式选择仍优先)', () => {
    expect(app).toMatch(/saved === '1' \|\| saved === '0'/)
    expect(app).toMatch(/\^\\\/stocks\\\/\//)
  })

  it('折叠态的主内容左内边距与展开态不同 —— 保证真让出了宽度', () => {
    expect(app).toContain("sidebarCollapsed ? 'md:pl-20' : 'md:pl-64'")
  })
})
