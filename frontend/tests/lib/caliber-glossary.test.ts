/**
 * 口径词典钉子(2026-09-18 用户给定的官方投教口径)。
 *
 * 这些词一旦被各处"顺手改写", 就会出现**同一指标两种说法** —— 比数字错更难发现。
 * 所以: ① 词典里的关键事实逐条钉死; ② 换词/漏词的改动必须是有意的(改测试=改口径)。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ANPAN, GS_SIGNAL, MINGPAN, gsBadge, glossaryTooltip } from '@panwatch/biz-ui'

describe('GS 信号: 机会/风险, 盘中实时', () => {
  it('一句话定义说清是 AI 多因子机会/风险 + 盘中实时', () => {
    expect(GS_SIGNAL.oneLine).toContain('AI')
    expect(GS_SIGNAL.oneLine).toContain('机会')
    expect(GS_SIGNAL.oneLine).toContain('风险')
    expect(GS_SIGNAL.oneLine).toContain('盘中实时')
  })

  it('G = 机会 + 防卖飞', () => {
    expect(GS_SIGNAL.g.label).toBe('G')
    expect(GS_SIGNAL.g.meaning).toContain('机会')
    expect(GS_SIGNAL.g.meaning).toContain('防卖飞')
  })

  it('S = 震荡或下跌 + 避深套', () => {
    expect(GS_SIGNAL.s.label).toBe('S')
    expect(GS_SIGNAL.s.meaning).toContain('震荡')
    expect(GS_SIGNAL.s.meaning).toContain('下跌')
    expect(GS_SIGNAL.s.meaning).toContain('避深套')
  })

  it('徽标文案由词典生成(各页不各写一版)', () => {
    expect(gsBadge('G')).toBe('G 机会 + 防卖飞')
    expect(gsBadge('S')).toBe('S 震荡或下跌 + 避深套')
  })
})

describe('明盘: 单笔 >30 万, 且散户也计入', () => {
  it('口径写明 30 万门槛', () => {
    expect(MINGPAN.oneLine).toContain('30 万')
    expect(MINGPAN.points.join(' ')).toContain('30 万')
  })

  it('点明局限: 散户 30 万也算, 不能代表真正的主力', () => {
    const all = `${MINGPAN.oneLine} ${MINGPAN.points.join(' ')} ${MINGPAN.caveat ?? ''}`
    expect(all).toContain('散户')
    expect(all).toMatch(/不能代表真正的主力|不代表真正的主力/)
  })
})

describe('暗盘: 拆单口径, 同一账户买入', () => {
  it('说清"大单拆成 30 万以下 → 不计入明盘"', () => {
    const all = `${ANPAN.oneLine} ${ANPAN.points.join(' ')}`
    expect(all).toMatch(/拆单|拆成/)
    expect(all).toContain('30 万以下')
    expect(all).toContain('不计入明盘')
  })

  it('点明"同一账户买入"是判定关键(不是看单子大小)', () => {
    expect(ANPAN.points.join(' ')).toContain('同一账户')
  })

  it('写明两套口径不可互校/不可相加', () => {
    expect(`${ANPAN.caveat ?? ''}`).toMatch(/不能互相校准|不能相互校准/)
    expect(`${ANPAN.caveat ?? ''}`).toMatch(/不能相加/)
  })
})

describe('使用纪律', () => {
  it('tooltip 把定义+要点+提醒都带上(页面只引用不重写)', () => {
    for (const e of [GS_SIGNAL, MINGPAN, ANPAN]) {
      const t = glossaryTooltip(e)
      expect(t).toContain(e.oneLine)
      for (const p of e.points) expect(t).toContain(p)
    }
  })

  it('关键口径只写在词典里(页面不得复制定义)', () => {
    const root = resolve(__dirname, '../..')
    const targets = [
      'src/pages/CaliberCompare.tsx',
      'src/pages/DarkFundTop.tsx',
      'src/pages/workbench/tabs/L2Tab.tsx',
      'packages/biz-ui/src/components/DecisionPioneerCard.tsx',
    ]
    // 这些是"定义句", 只能出现在词典模块
    const definitionSentences = [
      '散户买卖 30 万也计入其中',
      '但这些单是同一账户买入的，这部分记为暗盘',
      'G 信号 = 机会 + 防卖飞',
    ]
    for (const rel of targets) {
      const src = readFileSync(resolve(root, rel), 'utf-8')
      for (const d of definitionSentences) {
        expect(src, `${rel} 复制了词典里的定义句`).not.toContain(d)
      }
    }
  })

  it('GS 卡片从词典取文案(不硬编码 G/S 含义)', () => {
    const card = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/components/DecisionPioneerCard.tsx'), 'utf-8')
    expect(card).toContain('GS_SIGNAL')
    expect(card).toContain('glossaryTooltip(GS_SIGNAL)')
    // 不允许再出现手写的含义串
    expect(card).not.toContain('G=机会+防卖飞')
  })
})
