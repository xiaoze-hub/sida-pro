// P1(2026-09-18): KlineChart 的分时接线 —— 源级断言。
//
// 为什么不渲染 KC 来测: 真渲染会创建 lightweight-charts 实例(jsdom 无 canvas), 而分时的
// **行为**(取数/轮询/降级/空态)已被 MinutePane 的 5 例测试覆盖(见 minute-pane.test.tsx)。
// 这里只钉"接线"本身, 重点是**默认不开**(旗舰页 StockWorkbench 行为不变)。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const KC = readFileSync(
  resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'),
  'utf-8',
)

describe('KlineChart 分时接线(P1)', () => {
  it('新增 enableMinute 可选 prop, 且文档写明默认关(不影响旗舰页)', () => {
    expect(KC).toMatch(/enableMinute\?: boolean/)
    expect(KC).toMatch(/默认 \*\*false\*\*/)
  })

  it('分时按钮只在 enableMinute 时出现(默认没有 → StockWorkbench 不变)', () => {
    expect(KC).toMatch(/\{props\.enableMinute && \(\s*<button\s*[\s\S]{0,200}?data-testid="minute-toggle"/)
  })

  it('分时模式挂载 MinutePane, 并隐藏 K 线容器(不卸载, 避免图表实例重建竞态)', () => {
    expect(KC).toMatch(/mode === 'minute' && props\.enableMinute \? 'hidden' : 'w-full'/)
    expect(KC).toMatch(/props\.enableMinute && mode === 'minute' && \(\s*<MinutePane/)
  })

  it('取数/轮询/四种状态收敛在 MinutePane(单一职责, 便于淘汰 IK 后复用)', () => {
    // KC 里不再直接 fetch 分时接口
    expect(KC).not.toContain('/quotes/minute/')
    const pane = readFileSync(
      resolve(__dirname, '../../packages/biz-ui/src/components/MinutePane.tsx'),
      'utf-8',
    )
    expect(pane).toContain('/quotes/minute/')
    expect(pane).toContain('pollMs = 30000')
    expect(pane).toContain('timeoutMs: 60000') // 冷启动留给 swings 全量翻页
    expect(pane).toContain('data-minute-empty') // 空态显式说明, 不补 0
  })
})
