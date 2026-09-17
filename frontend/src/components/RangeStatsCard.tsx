/**
 * 设计稿 v2.1 §10.2④ —— 区间统计卡(资金面板顶部那一行/"区间统计")。
 *
 * 数据由 `KlineChart` 的 `onRangeStats` 回调给出(只有图表同时持有 K线/资金柱/事件三份数据),
 * 本组件**只负责渲染**, 不做任何取数或推算 —— 所以不会有"卡上一套口径、图里另一套口径"的漂移。
 *
 * 诚实口径(与全仓纪律一致):
 *  - 明盘/暗盘累计为 `null`(区间内该口径无数据) → 显示 `--`, **不显示 0**;
 *  - 累计值附带"有值天数"(如 `+1.23亿 / 12天`), 让人看得出这个数是几天堆出来的;
 *  - 事件 0 次也照实显示 `0 次`(0 是事实, 不是缺数据)。
 */

import type { KlineRangeStats } from '@panwatch/biz-ui/components/KlineChart'
import { safeFixed, toAmount } from '@/lib/format'

/** 事件 kind → 中文短标(与 klineEvents 的 KIND_LABEL 同源口径, 这里只做展示收窄) */
const KIND_SHORT: Readonly<Record<string, string>> = {
  limit_up: '涨停',
  limit_down: '跌停',
  dragon_tiger: '龙虎榜',
  announcement: '公告',
  split_cluster: '拆单簇',
  cancel_anomaly: '撤单异常',
  support: '托盘',
  pressure: '压盘',
  unlock: '解套盘',
  my_trade: '我的买卖点',
}

function kindLabel(k: string): string {
  return KIND_SHORT[k] || k
}

/** 涨跌幅/振幅的符号色: 涨红跌绿(A 股惯例, 与 --stock-up/down 同源) */
function pctClass(v: number): string {
  if (v > 0) return 'text-stock-up'
  if (v < 0) return 'text-stock-down'
  return 'text-muted-foreground'
}

function pctText(v: number): string {
  return `${v > 0 ? '+' : ''}${safeFixed(v, 2)}%`
}

export interface RangeStatsCardProps {
  stats: KlineRangeStats
  /** 关闭该卡(仅在当前可视区间内隐藏, 下次区间变化会重新出现) */
  onClear?: () => void
}

export default function RangeStatsCard({ stats, onClear }: RangeStatsCardProps) {
  const eventDetail = Object.entries(stats.eventCounts)
    .map(([k, n]) => `${kindLabel(k)}×${n}`)
    .join(' · ')

  return (
    <div
      data-testid="range-stats-card"
      className="rounded border border-border/60 bg-card/60 p-2 text-[11px] leading-5"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-foreground">
          区间统计
          <span className="ml-1 font-normal text-muted-foreground">
            {stats.from} → {stats.to} · {stats.bars} 根
          </span>
        </span>
        {onClear && (
          <button
            type="button"
            onClick={onClear}
            aria-label="收起区间统计"
            className="shrink-0 rounded px-1 text-muted-foreground hover:bg-accent/40 hover:text-foreground"
          >
            ×
          </button>
        )}
      </div>
      <div className="mt-0.5 flex flex-wrap gap-x-3">
        <span className="text-muted-foreground">
          首末 <span className="font-mono text-foreground">{safeFixed(stats.firstClose)}</span> →{' '}
          <span className="font-mono text-foreground">{safeFixed(stats.lastClose)}</span>
        </span>
        <span className={pctClass(stats.changePct)}>涨跌幅 {pctText(stats.changePct)}</span>
        <span className="text-muted-foreground">振幅 {safeFixed(stats.amplitudePct, 2)}%</span>
      </div>
      <div className="flex flex-wrap gap-x-3">
        <span className="text-muted-foreground">
          明盘累计{' '}
          <span className="font-mono text-foreground">{toAmount(stats.mingNet)}</span>
          <span className="ml-1 text-[10px]">
            {stats.mingDays > 0 ? `${stats.mingDays}天` : '无数据'}
          </span>
        </span>
        <span className="text-muted-foreground">
          暗盘累计{' '}
          <span className="font-mono text-foreground">{toAmount(stats.darkNet)}</span>
          <span className="ml-1 text-[10px]">
            {stats.darkDays > 0 ? `${stats.darkDays}天` : '无数据'}
          </span>
        </span>
      </div>
      <div className="flex flex-wrap gap-x-3 text-muted-foreground">
        <span>
          事件 <span className="text-foreground">{stats.eventTotal} 次</span>
          {eventDetail ? <span className="ml-1 text-[10px]">({eventDetail})</span> : null}
        </span>
        {stats.priceLines.length > 0 && (
          <span>
            区间内价位线 <span className="text-foreground">{stats.priceLines.length} 条</span>
          </span>
        )}
      </div>
    </div>
  )
}
