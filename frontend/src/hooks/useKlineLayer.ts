/**
 * 设计稿 v2.1 §5 —— K 线图层数据取数 hook(供 `KlineChart` 这类"图层全靠 props"的图表用; P3: InteractiveKline 已删)。
 *
 * 背景(2026-09-18 审计断链): 后端 `/klines/{symbol}/summary` 早就产出
 * `gs_signals / fund_flow / events / unlock_levels / activity_series`, 图表组件也实现了
 * L2/L3/L4 图层与开关, 但**没有页面把数据传进去** ⇒ 六图层在生产里一条都没画出来。
 *
 * 本 hook 就是那次断链的补丁: 页面调一次, 拿到可直接展开进图表的 props 包。
 * (`KlineChart` 走的是另一条路 —— 它自己按需取, 因为它没有父组件传图层的调用方。)
 *
 * 诚实口径:
 *  - 事件/价位线一律过 `normalizeKlineEvents` / `normalizePriceLines` 白名单过滤, 脏点不进图;
 *  - 取数失败 → 返回空数组(**不编造**), 图表自然不画, 页面也不假报"已加载"。
 */

import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import {
  normalizeKlineEvents,
  normalizePriceLines,
  type KlineEventPoint,
  type KlinePriceLine,
} from '@panwatch/biz-ui/klineEvents'
import type { ActivityPoint } from '@panwatch/biz-ui/components/KlineChart'
import type { FundFlowBar } from '@panwatch/biz-ui/components/KlineChart'
/**
 * GS 买卖点(hook 的输出契约): **price 必填** —— 本 hook 负责"缺价格的点不喂图"(不补 0),
 * 所以这里比 `KlineChart` 的宽松版(price 可选)更严。原先借用 InteractiveKline 的类型,
 * 该组件已按评估退役(P3, 2026-09-18), 故把契约就地声明清楚。
 */
export type LayeredGsSignal = {
  date: string
  /** 'G' = 买入(MA5 上穿), 'S' = 卖出(MA5 下穿) */
  side: 'G' | 'S'
  confirmed: boolean
  price: number
}

interface SummaryLayerResponse {
  gs_signals?: Array<{ date: string; side: 'G' | 'S'; confirmed?: boolean; price?: number | null }> | null
  fund_flow?: FundFlowBar[] | null
  events?: Array<{ date?: string | null; kind?: string | null; label?: string | null }> | null
  unlock_levels?: Array<{ price?: number | null; kind?: string | null; label?: string | null }> | null
  activity_series?: ActivityPoint[] | null
}

export interface KlineLayerProps {
  gsSignals: LayeredGsSignal[]
  fundFlow: FundFlowBar[]
  events: KlineEventPoint[]
  supportPressure: KlinePriceLine[]
  activitySeries: ActivityPoint[]
  /** 取数是否完成(false 时上层可显示骨架/不动) */
  loaded: boolean
}

const EMPTY: KlineLayerProps = {
  gsSignals: [],
  fundFlow: [],
  events: [],
  supportPressure: [],
  activitySeries: [],
  loaded: false,
}

/**
 * @param symbol 标的代码
 * @param market 市场('CN' 等); 非 A 股后端不产图层数据(仍是空数组, 不是错误)
 * @param enabled 闸门(如"只有个股视图才取"), 关闭时**不发请求**
 */
export function useKlineLayer(symbol: string, market: string, enabled = true): KlineLayerProps {
  const [layer, setLayer] = useState<KlineLayerProps>(EMPTY)

  useEffect(() => {
    if (!enabled || !symbol) {
      setLayer(EMPTY)
      return
    }
    let alive = true
    setLayer(EMPTY)
    const load = async () => {
      try {
        const res = await fetchAPI<SummaryLayerResponse>(
          `/klines/${encodeURIComponent(symbol)}/summary?market=${encodeURIComponent(market)}`,
        )
        if (!alive) return
        const rawGs = Array.isArray(res?.gs_signals) ? res.gs_signals : []
        setLayer({
          gsSignals: rawGs.filter(
            (g): g is LayeredGsSignal =>
              !!g &&
              typeof g.date === 'string' &&
              (g.side === 'G' || g.side === 'S') &&
              // price 必填(本 hook 的输出契约) —— 缺失的点不喂图, 不补 0
              typeof g.price === 'number' &&
              Number.isFinite(g.price),
          ),
          fundFlow: Array.isArray(res?.fund_flow) ? res.fund_flow : [],
          events: normalizeKlineEvents(res?.events),
          supportPressure: normalizePriceLines(res?.unlock_levels),
          activitySeries: Array.isArray(res?.activity_series) ? res.activity_series : [],
          loaded: true,
        })
      } catch {
        // 失败 = 本次无图层(空数组), 不编造; 保持 loaded=true 让上层不再等
        if (alive) setLayer({ ...EMPTY, loaded: true })
      }
    }
    void load()
    return () => {
      alive = false
    }
  }, [symbol, market, enabled])

  return layer
}
