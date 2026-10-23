// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import {
  buildWeightRows,
  judgeOutcomeByDirection,
  OutcomeBadge,
  resolveOutcome,
  summarizeOutcomes,
  WeightTransparency,
} from '../Forecast'

/**
 * Forecast 页两条 P0 回归(2026-10-24, docs/design/20261023_04 §1.2 / §1.6)。
 *
 *  ① `no_data`(到期但取不到行情)必须是**独立中性态** —— 不得被画成 `✗` 未命中,
 *     也不得计入命中率分母;`pending`(未到期)同样排除在分母外。
 *  ② 权重区**禁止硬编码兜底** —— `/forecast/weights` 没返回时不得出现任何具体数字
 *     (旧实现回落 kronos 0.4414 / chronos 0.3379 / xgboost 0.1103 / linreg 0.1103,
 *      四行刚好合计 100%, 是「假精确」)。
 *
 * 只测纯判定函数(渲染层同一函数驱动), 不渲染整页。
 */

describe('resolveOutcome: 到期对照四态', () => {
  it('no_data 保持 no_data, 不落到 miss', () => {
    expect(resolveOutcome({ outcome_status: 'no_data' })).toBe('no_data')
  })

  it('no_data 即使同时带了 outcome_return_pct 也不改判 hit/miss', () => {
    // 后端契约: 方向字段缺失时仍返回已算出的涨跌幅 + no_data → 仍是无数据
    expect(resolveOutcome({ outcome_status: 'no_data', outcome_return_pct: 3.2, direction: 'up' })).toBe('no_data')
    expect(resolveOutcome({ outcome_status: 'no_data', outcome_return_pct: -1.7, direction: 'down' })).toBe('no_data')
  })

  it('pending / hit / miss 原样透传', () => {
    expect(resolveOutcome({ outcome_status: 'pending' })).toBe('pending')
    expect(resolveOutcome({ outcome_status: 'hit', outcome_return_pct: 1.1 })).toBe('hit')
    expect(resolveOutcome({ outcome_status: 'miss', outcome_return_pct: -2.4 })).toBe('miss')
  })

  it('未知状态串按无数据处理, 不退化成 hit/miss', () => {
    expect(resolveOutcome({ outcome_status: 'skipped' })).toBe('no_data')
  })

  it('后端没给 outcome 字段 → none(不是一次评估)', () => {
    expect(resolveOutcome({})).toBe('none')
    expect(resolveOutcome({ direction: 'up' })).toBe('none')
  })

  it('后端只给涨跌幅时按方向推算, 方向不可识别 → no_data', () => {
    expect(resolveOutcome({ direction: 'up', outcome_return_pct: 1.2 })).toBe('hit')
    expect(resolveOutcome({ direction: 'down', outcome_return_pct: 1.2 })).toBe('miss')
    expect(resolveOutcome({ direction: 'flat', outcome_return_pct: 0 })).toBe('hit')
    // 与后端 _judge_status 一致: up 遇 0.00% 判 miss(方向未兑现)
    expect(resolveOutcome({ direction: 'up', outcome_return_pct: 0 })).toBe('miss')
    // 方向字段缺失 → 无法判定, 显式无数据
    expect(resolveOutcome({ outcome_return_pct: 1.2 })).toBe('no_data')
  })

  it('judgeOutcomeByDirection: 无涨跌幅(undefined/null/NaN) → no_data', () => {
    expect(judgeOutcomeByDirection('up', undefined)).toBe('no_data')
    expect(judgeOutcomeByDirection('up', Number.NaN)).toBe('no_data')
    expect(judgeOutcomeByDirection(undefined, 1)).toBe('no_data')
  })
})

describe('summarizeOutcomes: 分母只含 hit/miss', () => {
  const items = [
    { outcome_status: 'hit', outcome_return_pct: 2.1, direction: 'up' },
    { outcome_status: 'miss', outcome_return_pct: -1.3, direction: 'up' },
    { outcome_status: 'no_data', direction: 'up' },
    { outcome_status: 'no_data', direction: 'up' },
    { outcome_status: 'pending', direction: 'up' },
    {},
  ]

  it('no_data 与 pending 不进分母, 单独计数', () => {
    const s = summarizeOutcomes(items)
    expect(s.hit).toBe(1)
    expect(s.miss).toBe(1)
    expect(s.noData).toBe(2)
    expect(s.pending).toBe(1)
    expect(s.judged).toBe(2) // ★ 不是 6, 也不是 4
    expect(s.hitRatePct).toBe(50)
  })

  it('全是 no_data / pending 时命中率是 null 而不是 0%', () => {
    const s = summarizeOutcomes([
      { outcome_status: 'no_data' },
      { outcome_status: 'pending' },
    ])
    expect(s.judged).toBe(0)
    expect(s.hitRatePct).toBeNull()
  })
})

describe('buildWeightRows: 只认真实返回值, 无硬编码兜底', () => {
  it('权重为 null(接口失败) → 空数组, 一个数字都不给', () => {
    expect(buildWeightRows(null)).toEqual([])
  })

  it('权重为空对象 → 空数组', () => {
    expect(buildWeightRows({})).toEqual([])
  })

  it('真实权重按固定顺序输出, 保留 1 位小数所需精度(不二次取整)', () => {
    const rows = buildWeightRows({
      linreg: 0.1103,
      kronos: 0.4414,
      xgboost: 0.25,
      chronos: 0.1,
      timesfm: 0.0983,
    })
    expect(rows.map(r => r.label)).toEqual(['Kronos', 'Chronos', 'XGB', '线性回归', 'TimesFM'])
    // 0.1103 → 11.03% → toFixed(1) = '11.0'(Math.round 会得到 11)
    expect(rows.find(r => r.key === 'linreg')!.pct.toFixed(1)).toBe('11.0')
    const sum = rows.reduce((a, r) => a + r.pct, 0)
    expect(sum).toBeCloseTo(100, 6)
  })

  it('只返回后端给了的模型(timesfm 缺位时不补 0 也不补默认)', () => {
    const rows = buildWeightRows({ kronos: 0.4, chronos: 0.3, xgboost: 0.2, linreg: 0.1 })
    expect(rows.map(r => r.key)).toEqual(['kronos', 'chronos', 'xgboost', 'linreg'])
    expect(rows.some(r => r.key === 'timesfm')).toBe(false)
  })

  it('非数值 / NaN 权重被跳过, 不显示 NaN%', () => {
    const rows = buildWeightRows({ kronos: 0.5, chronos: Number.NaN, xgboost: 'x' as unknown as number })
    expect(rows.map(r => r.key)).toEqual(['kronos'])
  })
})

afterEach(cleanup)

describe('OutcomeBadge 渲染: no_data 不得画成 ✗', () => {
  it('no_data → 「无数据」灰虚线印章, 页面不出现 ✗ / ✓', () => {
    const { container } = render(<OutcomeBadge item={{ outcome_status: 'no_data', direction: 'up', expected_pct: 1.8 }} />)
    expect(container.textContent).toContain('无数据')
    expect(container.textContent).not.toContain('✗')
    expect(container.textContent).not.toContain('✓')
    // 中性态: 不带涨跌色
    expect(container.innerHTML).not.toMatch(/text-stock-up|text-stock-down|rose|emerald/)
  })

  it('no_data 即便带了涨跌幅也只显示「无数据」, 不显示 ✓/✗', () => {
    const { container } = render(
      <OutcomeBadge item={{ outcome_status: 'no_data', outcome_return_pct: 2.4, direction: 'up', expected_pct: 1.8 }} />
    )
    expect(container.textContent).toContain('无数据')
    expect(container.textContent).toContain('vs 实际+2.4%')
    expect(container.textContent).not.toContain('✗')
  })

  it('hit / miss 仍是 ✓ / ✗(仅这两态出对错结论)', () => {
    const hit = render(<OutcomeBadge item={{ outcome_status: 'hit', outcome_return_pct: 1.2, expected_pct: 0.8 }} />)
    expect(hit.container.textContent).toContain('✓')
    hit.unmount()
    const miss = render(<OutcomeBadge item={{ outcome_status: 'miss', outcome_return_pct: -1.2, expected_pct: 0.8 }} />)
    expect(miss.container.textContent).toContain('✗')
  })

  it('pending → 「待验证」, 既无 ✓ 也无 ✗', () => {
    const { container } = render(<OutcomeBadge item={{ outcome_status: 'pending', direction: 'up', expected_pct: 1 }} />)
    expect(container.textContent).toBe('待验证')
  })
})

describe('WeightTransparency 渲染: 无接口数据时不得出现数字', () => {
  it('unavailable → 「权重不可用（接口未就绪）」+ 快照免责小字, 且无任何百分比', () => {
    const { container } = render(<WeightTransparency weights={null} source="" state="unavailable" />)
    expect(screen.getByText('权重不可用（接口未就绪）')).toBeTruthy()
    expect(screen.getByText('历史参考快照仅用于回测，不代表当前权重')).toBeTruthy()
    // ★ 核心断言: 旧实现这里会渲染 Kronos 44% / Chronos 34% / XGB 11% / 线性回归 11%
    expect(container.textContent).not.toMatch(/\d+%/)
    expect(container.textContent).not.toMatch(/44|34|11%|0\.4414/)
  })

  it('loading → 「权重加载中…」, 不摆数字也不摆免责小字', () => {
    const { container } = render(<WeightTransparency weights={null} source="" state="loading" />)
    expect(screen.getByText('权重加载中…')).toBeTruthy()
    expect(container.textContent).not.toMatch(/\d+%/)
    expect(container.textContent).not.toContain('历史参考快照')
  })

  it('ok → 只渲染真实权重(5 支), 1 位小数并报出合计', () => {
    const { container } = render(
      <WeightTransparency
        weights={{ kronos: 0.4414, chronos: 0.3379, xgboost: 0.1103, linreg: 0.1103, timesfm: 0.0001 }}
        source="history"
        state="ok"
      />
    )
    const text = container.textContent || ''
    expect(text).toContain('Kronos 44.1%')
    expect(text).toContain('TimesFM 0.0%')
    expect(text).toContain('已返回 5 支 · 合计 100.0%')
    expect(text).toContain('实时回测统计')
  })
})
