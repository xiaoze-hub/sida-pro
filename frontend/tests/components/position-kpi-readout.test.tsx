// @vitest-environment jsdom
/**
 * 持仓 KPI 读数不被截断 —— 2026-10-24（交接文档第 3 项）。
 *
 * 现场症状：KPI 数值被截成 `+2.1…`。
 * 实测根因有两条，**不是**「数字用 truncate」这一条能概括的：
 *   ① `Stat` 数值那一行同时挂了 `truncate` —— 数字被省略号切掉，读数直接失真（零容忍）；
 *   ② 副标签 `上一交易日 10-23`（12 字）在 6 栏网格/账户行里换行，把数值挤成一行半高。
 *
 * 修法：数值 `whitespace-nowrap` 永不截断；副标签单行 + 缩短为 `上日 10-23`。
 * 这里把「标签文案」和「数值不截断」两条都钉住。
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import Stat from '@panwatch/biz-ui/components/Stat'
import { dailyPnlDisplayLabel } from '@/pages/stocks/shared'

const meta = (
  period: 'today' | 'previous_trading_day' | 'mixed' | 'unknown',
  date: string | null = null,
) => ({
  daily_pnl_period: period,
  daily_pnl_label:
    period === 'today'
      ? '今日盈亏'
      : period === 'previous_trading_day'
        ? '上一交易日盈亏'
        : period === 'mixed'
          ? '最近交易日盈亏'
          : '当日盈亏',
  daily_pnl_date: date,
})

describe('副标签：紧凑档必须短到不换行', () => {
  it('上一交易日 → 「上日 10-23」（保留日期，长度减 1/3）', () => {
    expect(dailyPnlDisplayLabel(meta('previous_trading_day', '2026-10-23'), true)).toBe('上日 10-23')
  })

  it('上一交易日但没日期 → 「上日盈亏」', () => {
    expect(dailyPnlDisplayLabel(meta('previous_trading_day', null), true)).toBe('上日盈亏')
  })

  it('今日/混合 保持不变', () => {
    expect(dailyPnlDisplayLabel(meta('today', '2026-10-24'), true)).toBe('今日')
    expect(dailyPnlDisplayLabel(meta('mixed', '2026-10-23'), true)).toBe('最近交易日')
  })

  it('全称档口径不变（其它页面依赖它，别一起改）', () => {
    expect(dailyPnlDisplayLabel(meta('previous_trading_day', '2026-10-23'))).toBe(
      '上一交易日盈亏 (10-23)',
    )
  })
})

describe('KPI 数值永不截断', () => {
  const valueDiv = (container: HTMLElement) =>
    container.querySelector('div.whitespace-nowrap.font-num') as HTMLElement | null

  it('数值行是 whitespace-nowrap 且没有 truncate', () => {
    const { container } = render(
      <Stat label="上日 10-23" value="+2.1万 (+2.31%)" tone="text-stock-up" />,
    )
    const div = valueDiv(container)
    expect(div).not.toBeNull()
    expect(div!.className).toContain('whitespace-nowrap')
    expect(div!.className).not.toContain('truncate')
    expect(div!.textContent).toBe('+2.1万 (+2.31%)')
  })

  it('标签仍是单行（truncate 保留），且带 title 全文', () => {
    const { container } = render(<Stat label="上日 10-23" value="--" />)
    const label = container.querySelector('div.truncate.text-\\[10px\\]') as HTMLElement | null
    expect(label).not.toBeNull()
    expect(label!.getAttribute('title')).toBe('上日 10-23')
    expect(label!.className).toContain('truncate')
  })
})
