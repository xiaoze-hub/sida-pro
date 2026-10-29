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

/**
 * 取出"marker / 价位线重绘 effect"的依赖标识符集合（只在该 effect 的依赖数组上认
 * `props.tradeMarkers` 这一个特征项）。断言按**集合**而不是书写顺序，避免与行为无关的
 * 顺序调整造成假红。
 */
function markerEffectDeps(source: string): string[] {
  const found = [...source.matchAll(/\},\s*\[([^\]\n]*)\]\)/g)]
    .map((m) => m[1].split(',').map((s) => s.trim()).filter(Boolean))
    .find((deps) => deps.includes('props.tradeMarkers'))
  if (!found) throw new Error('未找到 marker/价位线重绘 effect 的依赖数组')
  return found
}

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

// ── P2 补搬(2026-09-18): 主力意图接线 ─────────────────────────────────────────
describe('KlineChart 主力意图接线(P2 补搬)', () => {
  it('支持 mainIntent prop, 且没给时会自取 summary(与 IK 原行为一致)', () => {
    expect(KC).toMatch(/mainIntent\?: MainIntentStructured \| null/)
    expect(KC).toMatch(/\/klines\/\$\{encodeURIComponent\(props\.symbol\)\}\/summary\?market=CN/)
    expect(KC).toMatch(/const intent = props\.mainIntent \?\? intentFetched/)
  })

  it('三种渲染都接上了: 箭头(markers)/筹码线(priceLine)/图例卡', () => {
    expect(KC).toContain('intentMarkersFor(')
    expect(KC).toContain('limitMoveMarkers(')
    expect(KC).toContain('intentPriceLinesFor(')
    expect(KC).toMatch(/data-testid="main-intent-legend"/)
    expect(KC).toMatch(/intentRenderable\(intent\) && intentLegend/)
  })

  it('数据不足时不给方向(图例只在 renderable 时出现, 文案逻辑在 lib 单测里钉死)', () => {
    expect(KC).toMatch(/intentRenderable\(intent\) && intentLegend/)
  })

  it('重绘依赖必须带 intent、标的/市场与新增图层入参, 否则切股后箭头/价位线/时间点标注不刷新', () => {
    // 2026-10-29 复审阻断 ①: 这里原先断言的是依赖数组的**字面量片段**
    // (`props.tradeMarkers, intent, props.symbol, props.market])`) —— 只要在中间插入一个
    // 新依赖(本次加了 `props.annotationMarkers, dataLen`)就假红, 而真正要钉的是
    // "这些标识符在依赖集合里"，与书写顺序无关。改成解析依赖数组后按集合断言。
    const deps = markerEffectDeps(KC)
    expect(deps).toEqual(
      expect.arrayContaining([
        'intent',
        'props.symbol',
        'props.market',
        'interval',
        'props.tradeMarkers',
        'props.annotationMarkers',
        'props.layersVisible',
        'props.priceLinesVisible',
      ]),
    )
  })
})

// ── P2 验收补回(2026-09-18): 均线读数 ──────────────────────────────────────────
describe('KlineChart 光标均线读数(迁移验收发现缺失后补回)', () => {
  it('光标读数含 MA5/MA10/MA20 三档, 且缺值显 --(不补数)', () => {
    expect(KC).toMatch(/ma5\?: number \| null/)
    expect(KC).toMatch(/MA5 \{hoverReadout\.ma5 == null \? '--' : safeFixed\(hoverReadout\.ma5\)\}/)
    expect(KC).toMatch(/MA20 \{hoverReadout\.ma20 == null \? '--'/)
  })

  it('均线值复用绘制时算好的数组, 不重复计算', () => {
    expect(KC).toMatch(/maValuesRef\.current = \{ ma5, ma10, ma20 \}/)
    expect(KC).toMatch(/const mv = maValuesRef\.current/)
  })
})
