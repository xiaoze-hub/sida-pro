/**
 * P1-1(2026-09-18) 题材页分层钉子。
 *
 * 实测(生产 DOM): /theme-mood 一屏 526 条 1px 边框、密度 9680 字/千像素 —— 其中 **516 条**来自
 * 右列"题材列表"逐行画的分隔线(516 行一次渲染)。这是设计稿 v3.0 说的"表格墙"。
 * 本测试钉住三件事, 防回退:
 *  ① 右列**默认只给主线 12 条**(分层第一层), 全量在展开之后;
 *  ② 右列**不再逐行画线**(留白分隔取代);
 *  ③ 展开/收起入口存在且文案说明条数。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const src = readFileSync(resolve(__dirname, '../..', 'src/pages/ThemeMood.tsx'), 'utf-8')

describe('题材页: 默认分层, 不是表格墙', () => {
  it('右列默认切片 12 条(主线), 不是全量渲染', () => {
    expect(src).toMatch(/const visibleThemes = showAllThemes \? items : items\.slice\(0, 12\)/)
    expect(src).toMatch(/\{visibleThemes\.map\(/)
  })

  it('右列不再逐行画 1px 分隔线(516 条行线是"表格墙"的根因)', () => {
    // 整个文件都不该再出现 divide-y —— 行与行之间靠留白与分组区分
    expect(src).not.toMatch(/divide-y/)
  })

  it('展开/收起入口在, 且文案给出条数(不是隐形的"更多")', () => {
    expect(src).toMatch(/setShowAllThemes/)
    expect(src).toMatch(/展开全部 \$\{items\.length\} 条题材/)
    expect(src).toMatch(/收起\(只看主线 12 条\)/)
  })

  it('分层不删功能: 展开后仍是同一份 items(不是另一份数据)', () => {
    expect(src).toMatch(/const items = resp\?\.items \?\? \[\]/)
    expect(src).toMatch(/showAllThemes \? items : items\.slice/)
  })
})
