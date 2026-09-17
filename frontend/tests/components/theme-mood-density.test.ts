// UI 走查 B3(2026-09-18): theme-mood 单页 16456px(≈18 屏)的回归。
//
// 实测(Playwright DOM 探针, 1600×900): 页面总高 16456px, 其中右侧题材列表 16028px ——
// 全市场板块(数百行)没有限高, 把整页连同矩阵一起拉长(flex 拉伸)。
// 修法: 右侧列 **列内滚动 + 粘性**, 表头 sticky。这里用源级断言钉住, 防止又被改回无限高。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const src = readFileSync(resolve(__dirname, '../../src/pages/ThemeMood.tsx'), 'utf-8')

describe('theme-mood 密度(B3)', () => {
  it('右侧题材列必须限高 + 列内滚动(否则整页被拉到十几屏)', () => {
    expect(src).toContain('xl:max-h-[calc(100vh-160px)]')
    expect(src).toContain('xl:overflow-y-auto')
  })

  it('右侧列在桌面端粘性, 滚动时保持可见', () => {
    expect(src).toContain('xl:sticky')
  })

  it('滚动容器内的表头 sticky(滚起来仍知道每列是什么)', () => {
    expect(src).toMatch(/className="sticky top-0 z-10 mb-1 grid grid-cols-\[1fr_56px_56px_44px\]/)
  })

  it('折叠按钮仍然只收左列表, 不整块隐藏矩阵', () => {
    expect(src).toContain("boardCollapsed ? 'hidden xl:hidden' : ''")
  })
})
