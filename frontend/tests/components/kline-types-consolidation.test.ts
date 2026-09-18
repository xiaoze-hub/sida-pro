// 类型收口回归(B5c-P0 立, P3 更新)。
//
// 收口原则: **该合的合(单一事实来源), 不该合的有据可查**(形状不同就各自保留 + 注释说明),
// 而不是"为了统一强行改形状"(那是破坏性改动)。
// P3(2026-09-18): InteractiveKline 已退役 → 断言对象改为存活的 KlineChart / 各 lib 模块。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const read = (p: string) => readFileSync(resolve(__dirname, '../../', p), 'utf-8')

const KC = read('packages/biz-ui/src/components/KlineChart.tsx')
const EVENTS = read('packages/biz-ui/src/klineEvents.ts')
const MINUTE = read('packages/biz-ui/src/lib/minute-types.ts')
const MAIN = read('packages/biz-ui/src/lib/main-intent-types.ts')

describe('K 线相关类型的单一来源(P3 后的现状)', () => {
  it('事件 kind 白名单只在 klineEvents.ts 一处定义', () => {
    expect(EVENTS).toContain('export const KLINE_EVENT_KINDS')
    expect(KC).not.toMatch(/export type KlineEventKind\s*=/)
  })

  it('分时类型只在 lib/minute-types.ts(图表组件不得反向互相 import 类型)', () => {
    expect(MINUTE).toContain('export type MinutePoint')
    expect(MINUTE).toContain('export interface MinuteSwings')
    expect(read('packages/biz-ui/src/components/MinuteLwcChart.tsx')).toMatch(
      /import type \{[^}]*MinuteSwings[^}]*\} from '\.\.\/lib\/minute-types'/,
    )
  })

  it('主力意图类型只在 lib/main-intent-types.ts', () => {
    expect(MAIN).toContain('export interface MainIntentStructured')
    expect(KC).toMatch(/import type \{ MainIntentStructured \} from '\.\.\/lib\/main-intent-types'/)
  })

  it('GsSignalPoint 的两套形状**有意并存**: 图表宽松(可选) / hook 严格(必填 price)', () => {
    // KlineChart: 后端可能缺 price → 可选
    expect(KC).toMatch(/export interface GsSignalPoint \{[\s\S]{0,200}?confirmed\?: boolean/)
    // useKlineLayer: 本 hook 负责"缺价格不喂图" → 严格
    const hook = read('src/hooks/useKlineLayer.ts')
    expect(hook).toMatch(/export type LayeredGsSignal = \{[\s\S]{0,220}?price: number/)
    expect(hook).toMatch(/typeof g\.price === 'number'/)
  })

  it('InteractiveKline 已退役: 不应再有**导入/渲染**它(注释里提历史可保留)', () => {
    for (const f of [
      'packages/biz-ui/src/components/KlineChart.tsx',
      'src/hooks/useKlineLayer.ts',
      'src/pages/workbench/IndexBody.tsx',
      'src/pages/AnalysisDetail.tsx',
      'src/pages/PaperTrading.tsx',
    ]) {
      const src = read(f)
      expect(src, `${f} 仍在 import InteractiveKline`).not.toMatch(/from '[^']*InteractiveKline'/)
      expect(src, `${f} 仍在渲染 <InteractiveKline`).not.toContain('<InteractiveKline')
    }
  })
})
