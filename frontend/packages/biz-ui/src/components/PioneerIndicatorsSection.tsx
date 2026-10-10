import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { safeFixed } from '@/lib/format'
import {
  PIONEER_NOT_ADVICE_TEXT,
  normalizeNiuxiong,
  normalizeTrendLine,
  type NiuxiongData,
  type PioneerSignal,
  type TrendLineData,
} from '../lib/pioneer-indicators'

/**
 * 决策先锋辅助指标数据面板节(2026-10-10 P3 UI 集成)。
 *
 * 挂在工作台「数智决策」卡(workbench/DecisionCard)**尾部**一节 —— 属"追加节", 不改工作台
 * 布局密度(右栏本就是可滚动列)。展示:
 *  ① 三线**现值**: 趋势操盘线(红/黄/绿) + 牛熊线(牛/马/买卖线) —— 取后端快照;
 *  ② 买卖点**枚举**: 趋势操盘线的买/卖点客观规则(证据, 非建议);
 *  ③ 参数**校准标注**: 后端 `calibration`(逆向近似待校准) + 客观标注免责。
 *
 * 数据源(与 K 线图层同源, 但职责不同 —— 这里出**读数**, 图层出**线**):
 *   - `GET /api/indicators/trend-line/{symbol}?market=CN`
 *   - `GET /api/indicators/niuxiong/{symbol}?market=CN`
 * 分时突破不在本节(它有自己的「突/积」提示条, 见 `KlineChart` 的 minute 条), 以免同端点两处取数。
 *
 * 纪律(与全站一致): 缺失/脏值 → `--` 或后端 `note` 原文, **绝不编造**; 请求失败保留中性
 * 「无数据」(不猜原因); 仅 CN 标的取数(非 CN 无此源)。
 */
export default function PioneerIndicatorsSection({
  symbol,
  market = 'CN',
}: {
  symbol: string
  market?: string
}) {
  const [trend, setTrend] = useState<TrendLineData | null>(null)
  const [niuxiong, setNiuxiong] = useState<NiuxiongData | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!symbol || market.toUpperCase() !== 'CN') {
      setTrend(null)
      setNiuxiong(null)
      return
    }
    let cancelled = false
    setFailed(false)
    setTrend(null)
    setNiuxiong(null)
    const q = `?market=${encodeURIComponent(market)}`
    Promise.all([
      fetchAPI(`/indicators/trend-line/${encodeURIComponent(symbol)}${q}`)
        .then((r) => normalizeTrendLine(r))
        .catch(() => null),
      fetchAPI(`/indicators/niuxiong/${encodeURIComponent(symbol)}${q}`)
        .then((r) => normalizeNiuxiong(r))
        .catch(() => null),
    ]).then(([t, n]) => {
      if (cancelled) return
      setFailed(t == null || n == null)
      setTrend(t)
      setNiuxiong(n)
    })
    return () => {
      cancelled = true
    }
  }, [symbol, market])

  const trendLines = trend?.available ? trend.lines : null
  const nxLines = niuxiong?.available ? niuxiong.lines : null
  const signals: PioneerSignal[] = trend ? [...trend.buyPoints, ...trend.sellPoints] : []
  const calibration = trend?.calibration || niuxiong?.calibration

  return (
    <div className="mt-2 border-t border-border/40 pt-2" data-testid="pioneer-indicators-section">
      <div className="mb-1 text-[11px] font-medium text-muted-foreground">
        辅助指标（趋势操盘线 / 牛熊线）
      </div>

      {/* ① 三线现值 */}
      <div className="flex flex-col gap-0.5 text-[11px]">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
          <span className="text-muted-foreground">操盘线</span>
          {trendLines ? (
            <>
              <span>
                红 <span className="font-mono text-[hsl(var(--gs-go))]">{safeFixed(trendLines.red)}</span>
              </span>
              <span>
                黄 <span className="font-mono text-[#eab308]">{safeFixed(trendLines.yellow)}</span>
              </span>
              <span>
                绿 <span className="font-mono text-[hsl(var(--gs-stop))]">{safeFixed(trendLines.green)}</span>
              </span>
              {trend?.trend ? <span className="text-muted-foreground">{trend.trend}</span> : null}
              {trend?.band?.state ? (
                <span className="text-muted-foreground">{trend.band.state}</span>
              ) : null}
            </>
          ) : (
            <span className="text-muted-foreground">{trend?.note || '无数据'}</span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5">
          <span className="text-muted-foreground">牛熊线</span>
          {nxLines ? (
            <>
              <span>
                牛 <span className="font-mono">{safeFixed(nxLines.bull)}</span>
              </span>
              <span>
                马 <span className="font-mono">{safeFixed(nxLines.horse)}</span>
              </span>
              <span>
                买卖线 <span className="font-mono">{safeFixed(nxLines.trade)}</span>
              </span>
              {niuxiong?.signal ? (
                <span
                  className={
                    niuxiong.signal === 'B'
                      ? 'text-[hsl(var(--gs-go))]'
                      : 'text-[hsl(var(--gs-stop))]'
                  }
                >
                  {niuxiong.signal === 'B' ? '金叉 B' : '死叉 S'}
                  {niuxiong.cross?.barsAgo != null ? ` · ${niuxiong.cross.barsAgo}日前` : ''}
                </span>
              ) : (
                <span className="text-muted-foreground">近期无交叉</span>
              )}
            </>
          ) : (
            <span className="text-muted-foreground">{niuxiong?.note || '无数据'}</span>
          )}
        </div>
      </div>

      {/* ② 买卖点枚举(证据, 非建议) */}
      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px]">
        <span className="text-muted-foreground">买卖点</span>
        {signals.length === 0 ? (
          <span className="text-muted-foreground">--</span>
        ) : (
          signals.map((s, i) => (
            <span
              key={`${s.signal}-${i}`}
              title={s.trigger || s.rule || ''}
              className={s.signal === 'buy' ? 'text-[hsl(var(--gs-go))]' : 'text-[hsl(var(--gs-stop))]'}
            >
              {s.signal === 'buy' ? '买' : '卖'}·{s.trigger || s.rule || '--'}
            </span>
          ))
        )}
      </div>

      {/* ③ 校准标注(逆向近似待校准) + 客观标注免责 */}
      <div className="mt-1 text-[10px] text-muted-foreground/70" data-testid="pioneer-calibration">
        {failed ? '辅助指标取数失败 · 暂不出读数' : calibration || '逆向近似待校准'} · {PIONEER_NOT_ADVICE_TEXT}
      </div>
    </div>
  )
}
