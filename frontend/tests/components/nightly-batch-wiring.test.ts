/**
 * 2026-09-22 夜批「五项收尾」的接线钉子。
 *
 * 为什么用源码级判据: 这五项的**逻辑**各有单测(alert-bus / useBatchCloses / 后端 closes),
 * 但"组件到底接没接上"单测测不到 —— 拆了接线而单测仍全绿是最典型的假通过。
 * 这里钉死"谁引用了谁"。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const read = (p: string) => readFileSync(resolve(__dirname, '../../', p), 'utf-8')

describe('① 列表行 sparkline', () => {
  it('持仓行与自选行都画了 sparkline, 且数据来自批量 hook(不是逐行请求)', () => {
    const acc = read('src/pages/stocks/AccountsSection.tsx')
    expect(acc).toContain('useBatchCloses(')
    expect(acc).toContain('sparkCloses[')
    const watch = read('src/pages/stocks/WatchlistSection.tsx')
    expect(watch).toContain('useBatchCloses(')
    expect(watch).toContain('sparkCloses[stock.symbol]')
  })
})

describe('② 会话消息进右栏(真实事件源)', () => {
  it('右栏渲染 AlertLog 且订阅共享 store', () => {
    const rail = read('packages/biz-ui/src/components/workbench/QuickRail.tsx')
    expect(rail).toContain('RailAlertLog')
    expect(rail).toContain('useAlerts()')
  })
  it('API 失败与数据源跃迁两个真实事件源都接上了', () => {
    expect(read('src/App.tsx')).toContain('onApiFailure(')
    expect(read('src/hooks/useSourceHealth.ts')).toContain('pushAlert(')
    expect(read('packages/api/src/client.ts')).toContain('reportFailure(')
  })
})

describe('③ 口径徽章 / 信号 chip 扫替', () => {
  it('资金口径面统一走 CaliberBadge', () => {
    for (const f of ['src/pages/Dashboard.tsx', 'src/pages/DarkFundTop.tsx', 'packages/biz-ui/src/components/AuctionAnomalyTab.tsx', 'src/pages/Developers.tsx']) {
      expect(read(f), f).toContain('CaliberBadge')
    }
  })
  it('机会页动作徽章不再用 Tailwind 默认色(rose/emerald/blue)', () => {
    const opp = read('src/pages/Opportunities.tsx')
    expect(opp).not.toContain('actionBadgeClass')
    expect(opp).toContain('<SignalChip')
    expect(opp).not.toContain("'bg-rose-500/15")
    expect(opp).not.toContain('regimeToneClass')
    expect(opp).toContain('const regimeTone =')
  })
  it('CaliberBadge 支持 label/title 覆盖; SignalChip 有 go/stop 动作色调', () => {
    expect(read('packages/biz-ui/src/components/CaliberBadge.tsx')).toMatch(/label\?: string[\s\S]*title\?: string/)
    const chip = read('packages/biz-ui/src/components/SignalChip.tsx')
    expect(chip).toContain("'go'")
    expect(chip).toContain("'stop'")
    expect(chip).toContain('--gs-go')
  })
})

describe('④ 持仓表键盘行协议', () => {
  it('表格行有行号 + useRowNav', () => {
    const acc = read('src/pages/stocks/AccountsSection.tsx')
    expect(acc).toContain('useRowNav(')
    expect(acc).toContain('data-row-index={rowIndexByKey.get(')
    expect(acc).toContain('rowNav.index ===')
  })
})

describe('⑤ 外观设置', () => {
  it('设置页有外观段, 且能搜到', () => {
    expect(read('src/pages/Settings.tsx')).toContain('<AppearanceSection />')
    expect(read('src/pages/settings/useSettingsDerived.ts')).toContain("'sec-appearance'")
    const sec = read('src/pages/settings/AppearanceSection.tsx')
    for (const k of ['setMode', 'setDensity', 'setDim', 'data-focus-mode']) expect(sec, k).toContain(k)
  })
})
