// B5c-P0(2026-09-18): K 线类型收口回归。
//
// 背景: `KlineEventKind` 的 10 种 kind 曾在 `klineEvents.ts` 与 `InteractiveKline.tsx`
// 各定义一份(重复契约, 后端加新 kind 时容易只改一边)。本轮把 IK 收口到 `../klineEvents`,
// 并**明确不合并**两个形状不同的类型(KlineEvent 是宽松输入, GsSignalPoint 两侧必填性不同),
// 这条测试就钉住"该合的合了、不该合的有据可查"。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const read = (p: string) => readFileSync(resolve(__dirname, '../../', p), 'utf-8')

const IK = read('packages/biz-ui/src/components/InteractiveKline.tsx')
const KC = read('packages/biz-ui/src/components/KlineChart.tsx')
const SHARED = read('packages/biz-ui/src/klineEvents.ts')

describe('K 线类型收口(P0)', () => {
  it('kind 白名单只在 klineEvents.ts 定义一处, 两个图表组件都不再自定义', () => {
    expect(SHARED).toContain('export const KLINE_EVENT_KINDS')
    for (const [name, src] of [
      ['InteractiveKline', IK],
      ['KlineChart', KC],
    ] as const) {
      expect(src, `${name} 不应再自定义 KlineEventKind 联合`).not.toMatch(
        /export type KlineEventKind\s*=\s*\n?\s*\|/,
      )
    }
  })

  it('InteractiveKline 从 klineEvents 引入 kind 与价位线类型', () => {
    expect(IK).toMatch(/import \{[^}]*KlineEventKind[^}]*\} from '\.\.\/klineEvents'/)
    expect(IK).toMatch(/import \{[^}]*KlinePriceLine[^}]*\} from '\.\.\/klineEvents'/)
  })

  it('SupportPressureLine 是 KlinePriceLine 的别名(形状一致, 只多可选 ratio)', () => {
    expect(IK).toMatch(/export type SupportPressureLine = KlinePriceLine/)
    expect(SHARED).toMatch(/export interface KlinePriceLine\s*\{[\s\S]*?price: number[\s\S]*?kind: 'support' \| 'pressure'/)
    expect(SHARED).toMatch(/ratio\?: number \| null/)
  })

  it('KlineEvent(宽松输入)与 GsSignalPoint 保持各自形状 —— 有注释说明为什么不合', () => {
    // KlineEvent: 仅收口 kind, label 仍可选(标准化的 KlineEventPoint 里 label/tone 必填)
    expect(IK).toMatch(/export type KlineEvent = \{[\s\S]{0,200}?label\?: string/)
    expect(SHARED).toMatch(/export interface KlineEventPoint[\s\S]{0,400}?label: string/)
    // 两个组件仍各自持有 GsSignalPoint(必填性不同, 合并即破坏性改动)
    expect(IK).toMatch(/export type GsSignalPoint = \{[\s\S]{0,200}?confirmed: boolean/)
    expect(KC).toMatch(/export interface GsSignalPoint \{[\s\S]{0,200}?confirmed\?: boolean/)
  })
})
