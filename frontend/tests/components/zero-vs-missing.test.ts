/**
 * P0-4(2026-09-18): "0 与无数据必须可分" —— 持仓页/首页的零值与缺数核查钉子。
 *
 * 核查结论(实测 + 代码取证):
 *  · 持仓页 4 处 `0.0` 形式显示**都是真实 0**(仓位 0.0% = 无持仓; 3 处 `(+0.00%)` = 盈亏确为 0),
 *    代码路径 `safeNum(...) === null ? '--' : ...` 本来就是 null-safe 的 —— **不是 bug**;
 *  · 但同批核出**两处真问题**(首页市场资金带): ① 缺数被 `?? 0` 当成 0 上"涨红"色;
 *    ② `safeFixed(x, 0, '0')` 把缺数显示成 **"0亿"**。两条都违反诚实口径, 本测试把它们钉住。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = resolve(__dirname, '../..')
const read = (p: string) => readFileSync(resolve(root, p), 'utf-8')

describe('持仓账户百分比: null 与 0 必须可分', () => {
  const src = read('src/pages/stocks/AccountsSection.tsx')

  it('null → "--"(不是 0.00%), 0 → 带符号的 +0.00%(不是 --)', () => {
    expect(src).toMatch(/safeNum\(account\.total_pnl_pct\) === null \? '--'/)
    expect(src).toMatch(/account\.total_pnl_pct >= 0 \? '\+' : ''/)
  })
})

describe('首页市场资金带: 缺数不许冒充 0', () => {
  const src = read('src/pages/Dashboard.tsx')

  it('主力净流入缺数走中性色 —— 不给"涨红"(0 与缺数在视觉上必须可分)', () => {
    expect(src).not.toMatch(/\(marketFlow\.total_main_flow \?\? 0\) >= 0 \? 'text-stock-up'/)
    expect(src).toMatch(/marketFlow\??\.total_main_flow == null\s*\n?\s*\? 'text-muted-foreground'/)
  })

  it('成交额缺数显示 "--", 不许显示 "0亿"', () => {
    expect(src).not.toMatch(/safeFixed\(marketFlow\.total_amount, 0, '0'\)/)
    expect(src).toMatch(/marketFlow\??\.total_amount == null \? '--'/)
  })

  it('全仓不再有"缺数回退到 0"的金额写法(抽查 safeFixed 第三参为 0 的写法)', () => {
    for (const f of ['src/pages/Dashboard.tsx', 'src/pages/stocks/AccountsSection.tsx']) {
      expect(read(f)).not.toMatch(/safeFixed\([^)]*,\s*'0'\s*\)/)
    }
  })
})

describe('P0-2 收尾: 首页「结论行」', () => {
  const src = read('src/pages/Dashboard.tsx')

  it('首屏有结论行(一行读完市场状态, 而不是一堆碎片)', () => {
    expect(src).toContain('data-testid="market-conclusion"')
    for (const k of ['市场情绪', '涨停', '封板率', '成交', '主力净流入', '口径']) {
      expect(src).toContain(k)
    }
  })

  it('结论行的每个数都走 `--` 兜底, 且**不新增请求**(只用本页已有 state)', () => {
    expect(src).toMatch(/phaseKpi\.limitUp \?\? '--'/)
    expect(src).toMatch(/marketFlow\?\.up_count \?\? '--'/)
    expect(src).toMatch(/marketFlow\?\.total_amount == null \? '--'/)
    // 2026-09-22: 口径标签换成统一徽章(CaliberBadge), 但**口径必须仍然可见** ——
    // 断言从"字面模板"改成"徽章 + 标签兜底": 有原始口径值给徽章, 后端只给中文标签就如实显示,
    // 两者的共同底线(结论行里看得到口径)不变。
    expect(src).toContain('<CaliberBadge caliber={caliberOf(marketFlow?.caliber)}')
    expect(src).toMatch(/label=\{marketFlow\?\.caliber_label/)
    expect(src).not.toMatch(/market-conclusion[\s\S]{0,600}\?\? 0/)
  })
})
