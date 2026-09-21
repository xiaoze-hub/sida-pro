/**
 * 从列表页跳进行情页后必须能返回(2026-09-20 用户报: "从持仓页面选择股票, 进入行情页,
 * 但是没有返回按钮, 返回持仓页, 不太方便")。
 *
 * 三级降级:
 *   ① 带来路(state) → 「← 返回持仓」, 点击回**那个具体页面**(不是 history 退一步)
 *   ② 无 state 但是站内跳来的(key !== 'default') → 「← 返回」= history 退一步
 *   ③ 直接输网址/刷新/收藏(首条历史) → **不渲染**(不给会退到站外的假按钮)
 *
 * 另有一条**全仓钉子**: 所有跳 `/stocks/:symbol` 的入口都必须带来路 —— 漏一处就又出现
 * "进了行情页没返回"的死角。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { navBackState } from '@/lib/nav-back'

const ROOT = resolve(__dirname, '../..')

function read(rel: string) {
  return readFileSync(resolve(ROOT, rel), 'utf-8')
}

describe('navBackState 契约', () => {
  it('把来路放进 router state', () => {
    expect(navBackState('/portfolio', '持仓')).toEqual({
      navBack: { path: '/portfolio', label: '持仓' },
    })
  })

  it('label 只放页面名 —— 按钮文案由 hook 拼「返回X」', () => {
    const st = navBackState('/dark-fund-top', '暗盘资金榜')
    expect(st.navBack!.label).toBe('暗盘资金榜')
    expect(st.navBack!.label.startsWith('返回')).toBe(false)
  })
})

describe('全仓钉子: 每个进行情页的入口都要带来路', () => {
  const ENTRY_FILES: [string, string][] = [
    ['src/pages/stocks/useStocksData.ts', '持仓页'],
    ['src/pages/Opportunities.tsx', '机会页'],
    ['src/pages/Dashboard.tsx', '首页'],
    ['src/pages/DarkFundTop.tsx', '暗盘资金榜'],
    ['src/pages/Heatmap.tsx', '板块热力图'],
  ]

  it.each(ENTRY_FILES)('%s (%s) 的跳转都带 navBackState', (file) => {
    const src = read(file)
    // 所有 "/stocks/ 或 /boards/ 的 navigate 调用
    const calls = src.match(/navigate\(`\/(stocks|boards)\/[^`]*`[^)]*\)/g) || []
    expect(calls.length).toBeGreaterThan(0)
    const bare = calls.filter((c) => !c.includes('navBackState('))
    expect(bare, `裸跳转(无来路)会让用户回不去:\n${bare.join('\n')}`).toEqual([])
  })

  it('工作台把 back 传给 HeaderBand(不接就等于没做)', () => {
    const src = read('src/pages/StockWorkbench.tsx')
    expect(src).toMatch(/const back = useBackTarget\(\)/)
    expect(src).toMatch(/back=\{back\}/)
    // hook 必须在 early return 之前调用(rules-of-hooks)
    const hookAt = src.indexOf('const back = useBackTarget()')
    const earlyReturn = src.indexOf('if (!symbol) return')
    expect(earlyReturn).toBeGreaterThan(-1)
    expect(hookAt).toBeLessThan(earlyReturn)
  })

  it('带1 渲染返回按钮, 且不传就不渲染(直接打开时不给假按钮)', () => {
    const src = read('packages/biz-ui/src/components/workbench/HeaderBand.tsx')
    expect(src).toMatch(/back\?: \{ label: string; onBack: \(\) => void \} \| null/)
    expect(src).toMatch(/\{back \? \(/)
    expect(src).toMatch(/onClick=\{back\.onBack\}/)
    expect(src).toMatch(/aria-label=\{back\.label\}/)
  })
})
