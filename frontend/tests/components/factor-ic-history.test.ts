/**
 * B9 收口钉子(前端): IC 时序迷你图**不许把"没算出来"画成 0**。
 *
 * 三条: ① 只有 ≥2 个已算出 IC 的点才连线(1 个点画线 = 编趋势);
 *      ② `ic === null` 的那天不进曲线(它是"样本不足", 不是 0);
 *      ③ 文案要分得开"还没有快照"和"快照不足"。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const raw = readFileSync(resolve(__dirname, '../../src/pages/FactorIC.tsx'), 'utf-8')
const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

describe('IC 时序迷你图', () => {
  it('点不足 2 个不画线, 显示 -- 并说明"快照不足"', () => {
    expect(src).toMatch(/series\.length < 2/)
    expect(src).toContain('快照不足')
    expect(src).toMatch(/>--</)
  })

  it('null 的 IC 不进曲线(不当作 0 拉平)', () => {
    expect(src).toMatch(/p\.ic === null \|\| p\.ic === undefined\) continue/)
    expect(src).not.toMatch(/p\.ic \|\| 0/)
  })

  it('区分"还没有快照"与"快照不足"两种说明', () => {
    expect(src).toContain('还没有 IC 快照')
    expect(src).toContain('快照不足（需 ≥2 天算出 IC 才画线）')
  })

  it('曲线带零基准虚线(IC 有正负, 没有基准线会误读方向)', () => {
    expect(src).toContain('strokeDasharray')
  })

  it('取的是 history 端点且与当前 horizon 对齐', () => {
    expect(src).toContain('factorICApi.history(horizon, 30)')
    expect(src).toContain('FactorICHistoryResp')
  })
})
