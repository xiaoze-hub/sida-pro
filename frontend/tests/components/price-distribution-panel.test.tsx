// @vitest-environment jsdom
/**
 * I-8 微观分价博弈矩阵面板(2026-10-18)
 *
 * 守四件事:
 * ① **结论由后端给**: 三条结论文本原样渲染(前端不重算价位/占比);
 * ② **红绿对比条 = 两个维度**: 条长按成交量占比、条内红绿按竞买率切分; 缺竞买率画中性灰条,
 *    **不**画成 100% 卖(缺失不是方向);
 * ③ **标记来自 tags**: 托举/抛压/拆单徽标只渲染后端给的 tag, 前端不重判;
 * ④ **缺数据不冒充**: available=false 只显示后端 reason; 失败时显式"显示上次成功值"。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import PriceDistributionPanel, {
  PriceDistributionLadder,
  type PriceDistribution,
} from '../../src/components/PriceDistributionPanel'

function fixture(over: Partial<PriceDistribution> = {}): PriceDistribution {
  return {
    available: true,
    symbol: '002361',
    market: 'CN',
    trade_date: '2026-10-16',
    snapshot_ts: '15:00:02',
    day: {
      total_vol: 67000,
      buy_vol: 39699,
      sell_vol: 27301,
      net_amount: 11158200,
      buy_ratio: 0.5925,
      big_ratio: 0.006,
      coverage: 1,
      vwap: 9.101,
    },
    bins: [
      {
        price: 35.32,
        volume: 30000,
        buy_vol: 21600,
        sell_vol: 8400,
        buy_ratio: 0.72,
        sell_ratio: 0.28,
        big_ratio: 0,
        net_amount: 15580000,
        day_share: 0.4478,
        tags: ['strong_support', 'iceberg_buy'],
      },
      {
        price: 34.62,
        volume: 12000,
        buy_vol: 9420,
        sell_vol: 2580,
        buy_ratio: 0.785,
        sell_ratio: 0.215,
        big_ratio: 0.0167,
        net_amount: 7200000,
        day_share: 0.1791,
        tags: ['strong_support'],
      },
      {
        price: 35.8,
        volume: 11000,
        buy_vol: 1969,
        sell_vol: 9031,
        buy_ratio: 0.179,
        sell_ratio: 0.821,
        big_ratio: 0,
        net_amount: -8100000,
        day_share: 0.1642,
        tags: ['strong_resistance'],
      },
    ],
    summary: [
      {
        key: 'support',
        icon: '🛡️',
        title: '主力强托举位',
        available: true,
        price: 34.62,
        ratio: 0.785,
        text: '34.62 (主动买占比 78.5%)',
      },
      {
        key: 'resistance',
        icon: '⚡',
        title: '密集抛压带',
        available: true,
        price: 35.8,
        ratio: 0.821,
        text: '35.80 (主动卖占比 82.1%)',
      },
      {
        key: 'iceberg',
        icon: '🕵️',
        title: '算法拆单提示',
        available: true,
        price: 35.32,
        ratio: 0.72,
        text: '在 35.32 附近检测到小单高频拆单吸筹(该档 30000 手, 大单占比仅 0.0%)',
      },
    ],
    bias: { code: 'net_buy', label: '主动买占优', net_amount: 11158200 },
    quality: {
      bins: 3,
      identity_ok_ratio: 1,
      big_data_present: true,
      coverage: 1,
      degraded: false,
      cross_check: null,
    },
    cache: { hit: true, source: 'db', age_s: 4, ttl_s: 15, stale: false },
    caliber: '腾讯分价表 appn=price',
    ...over,
  }
}

afterEach(cleanup)

describe('PriceDistributionPanel', () => {
  it('renders the three backend conclusions verbatim', () => {
    render(<PriceDistributionPanel data={fixture()} loading={false} failed={false} lastOkAt="" />)

    expect(screen.getByTestId('price-dist-summary-support').textContent).toContain(
      '34.62 (主动买占比 78.5%)',
    )
    expect(screen.getByTestId('price-dist-summary-resistance').textContent).toContain(
      '35.80 (主动卖占比 82.1%)',
    )
    expect(screen.getByTestId('price-dist-summary-iceberg').textContent).toContain(
      '在 35.32 附近检测到小单高频拆单吸筹',
    )
  })

  it('splits each ladder bar by buy/sell ratio and shows volume share', () => {
    render(<PriceDistributionPanel data={fixture()} loading={false} failed={false} lastOkAt="" />)

    const rows = screen.getAllByTestId('price-dist-row')
    expect(rows).toHaveLength(3)
    // 价格降序: 35.80 → 35.32 → 34.62
    expect(rows[0].getAttribute('data-price')).toBe('35.8')

    const buys = screen.getAllByTestId('price-dist-bar-buy')
    expect(buys[0].getAttribute('style')).toContain('width: 17.9%') // 35.80 竞买 17.9%
    expect(screen.getAllByTestId('price-dist-bar-sell')[0].getAttribute('style')).toContain(
      'width: 82.1%',
    )
    expect(screen.getByTestId('price-dist-ladder').textContent).toContain('44.8%')
    expect(screen.getByTestId('price-dist-ladder').textContent).toContain('托举')
    expect(screen.getByTestId('price-dist-ladder').textContent).toContain('抛压')
    expect(screen.getByTestId('price-dist-ladder').textContent).toContain('拆单吸筹')
  })

  it('draws a neutral bar instead of a fake 100% sell when ratio is missing', () => {
    const data = fixture({
      bins: [{ price: 9.05, volume: 30000, buy_ratio: null, sell_ratio: null, day_share: 1 }],
    })
    render(<PriceDistributionLadder bins={data.bins ?? []} />)
    expect(screen.getByTestId('price-dist-bar-unknown')).toBeTruthy()
    expect(screen.queryByTestId('price-dist-bar-buy')).toBeNull()
    expect(screen.queryByTestId('price-dist-bar-sell')).toBeNull()
  })

  it('caps the ladder to maxBins rows', () => {
    const bins = Array.from({ length: 40 }, (_, i) => ({
      price: 10 + i * 0.01,
      volume: 1000,
      buy_ratio: 0.5,
      day_share: 0.01,
    }))
    render(<PriceDistributionLadder bins={bins} maxBins={5} />)
    expect(screen.getAllByTestId('price-dist-row')).toHaveLength(5)
  })

  it('shows backend reason only when the matrix is unavailable', () => {
    render(
      <PriceDistributionPanel
        data={fixture({ available: false, reason: '腾讯分价表无数据(停牌/非交易时段/源不可用)' })}
        loading={false}
        failed={false}
        lastOkAt=""
      />,
    )
    expect(screen.getByTestId('price-dist-unavailable').textContent).toContain('腾讯分价表无数据')
    expect(screen.queryByTestId('price-dist-ladder')).toBeNull()
  })

  it('marks stale snapshots explicitly instead of pretending freshness', () => {
    render(
      <PriceDistributionPanel
        data={fixture({ cache: { hit: true, source: 'db_stale', stale: true, reason: '腾讯超时' } })}
        loading={false}
        failed={false}
        lastOkAt="14:32:10"
      />,
    )
    const stale = screen.getByTestId('price-dist-stale')
    expect(stale.textContent).toContain('显示上次成功值')
    expect(stale.textContent).toContain('腾讯超时')
  })

  it('renders the loading / failed empty states without inventing data', () => {
    const { unmount } = render(
      <PriceDistributionPanel data={null} loading={true} failed={false} lastOkAt="" />,
    )
    expect(screen.getByTestId('price-dist-empty').textContent).toBe('加载中…')
    unmount()

    render(<PriceDistributionPanel data={null} loading={false} failed={true} lastOkAt="14:00:00" />)
    expect(screen.getByTestId('price-dist-empty').textContent).toContain('上次成功 14:00:00')
  })
})
