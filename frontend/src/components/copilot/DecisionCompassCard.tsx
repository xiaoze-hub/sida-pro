import React from 'react'
import { Crosshair, ArrowUpRight, TrendingUp, TrendingDown, Bell } from 'lucide-react'
import { safeFixed } from '@/lib/format'

export interface DecisionCompassProps {
  ticker?: string
  tickerName?: string
  currentPrice?: number
  costPrice?: number
  supportPrice: number
  resistancePrice: number
  biasLabel?: string
  confidence?: number
  tactics?: string[]
  onDrawOnChart?: () => void
  onSetAlert?: (price: number) => void
  onCopy?: () => void
}

/**
 * 数智裁决指标罗盘组件 (SIDA Pro 顶级金融终端版)
 *
 * 将散落的文本支撑、阻力与盈亏信息结构化为直观的水平价位罗盘轴，
 * 支持一键在 K 线图叠加画线与设置破位告警。
 * 纯真实数据驱动：无默认伪造价格或虚构置信度。
 */
export const DecisionCompassCard: React.FC<DecisionCompassProps> = ({
  ticker,
  tickerName,
  currentPrice,
  costPrice,
  supportPrice,
  resistancePrice,
  biasLabel = '多空裁决',
  confidence,
  tactics = [],
  onDrawOnChart,
  onSetAlert,
  onCopy,
}) => {
  // 计算现价与成本在区间内的百分比位置
  const validPrices: number[] = [supportPrice, resistancePrice]
  if (currentPrice != null && Number.isFinite(currentPrice)) validPrices.push(currentPrice)
  if (costPrice != null && Number.isFinite(costPrice)) validPrices.push(costPrice)

  const minVal = Math.min(...validPrices) * 0.98
  const maxVal = Math.max(...validPrices) * 1.02
  const range = maxVal - minVal || 1

  const toPct = (val: number) => Math.min(Math.max(((val - minVal) / range) * 100, 4), 96)

  const supportPct = toPct(supportPrice)
  const resistancePct = toPct(resistancePrice)
  const currentPct = currentPrice != null && Number.isFinite(currentPrice) ? toPct(currentPrice) : null
  const costPct = costPrice != null && Number.isFinite(costPrice) ? toPct(costPrice) : null

  const pnlPct =
    costPrice != null && currentPrice != null && costPrice > 0
      ? safeFixed(((currentPrice - costPrice) / costPrice) * 100, 2)
      : null
  const isLoss = pnlPct !== null && Number(pnlPct) < 0

  return (
    <div data-testid="copilot-decision-compass" className="card-surface rounded-lg bg-s2 overflow-hidden my-2 text-[12px]">
      {/* 头部：综合裁决徽章与置信度 */}
      <div className="flex items-center justify-between border-b border-border bg-s3 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-1.5 rounded-md bg-role-watch/20 px-2 py-0.5 font-medium text-role-watch text-[12px]">
            <span className="h-1.5 w-1.5 rounded-full bg-role-watch" />
            {biasLabel}
          </span>
          {confidence != null ? (
            <span className="text-[12px] text-muted-foreground">
              置信度 <strong className="text-foreground font-mono">{confidence}%</strong>
            </span>
          ) : null}
        </div>
        <span className="text-[12px] font-mono text-role-watch flex items-center gap-1">
          <Crosshair className="w-3.5 h-3.5" />
          多智能体加权裁决
        </span>
      </div>

      {/* 中部：关键价位区间可视化罗盘 */}
      <div className="border-b border-border bg-s1 p-3 space-y-2">
        <div className="flex items-center justify-between text-[12px]">
          <span className="text-muted-foreground font-medium">
            {tickerName ? `${tickerName}${ticker ? ` (${ticker})` : ''}` : ticker || '关键点位'} 价位罗盘与多空分界
          </span>
          {pnlPct != null ? (
            <span
              className={`font-mono font-semibold flex items-center gap-1 ${
                isLoss ? 'text-stock-down' : 'text-stock-up'
              }`}
            >
              {isLoss ? <TrendingDown className="w-3.5 h-3.5" /> : <TrendingUp className="w-3.5 h-3.5" />}
              持仓盈亏: {pnlPct}%
            </span>
          ) : null}
        </div>

        {/* 水平区间轴 */}
        <div className="py-2.5">
          <div className="relative h-2 w-full rounded-full bg-s3 flex items-center">
            {/* 支撑位标记 */}
            <div
              className="absolute h-2.5 w-2.5 -ml-1 rounded-full text-stock-down bg-current"
              style={{ left: `${supportPct}%` }}
              title={`防守支撑位: ${safeFixed(supportPrice, 2)}`}
            />
            {/* 现价点位 (若有) */}
            {currentPct != null && (
              <div
                className="absolute h-3.5 w-3.5 -ml-1.5 rounded-full bg-role-watch z-10"
                style={{ left: `${currentPct}%` }}
                title={`现价: ${safeFixed(currentPrice, 2)}`}
              />
            )}
            {/* 成本位标记 (若有) */}
            {costPct != null && (
              <div
                className="absolute h-2.5 w-2.5 -ml-1 rounded-full bg-amber-500"
                style={{ left: `${costPct}%` }}
                title={`持仓成本价: ${safeFixed(costPrice, 2)}`}
              />
            )}
            {/* 阻力位标记 */}
            <div
              className="absolute h-2.5 w-2.5 -ml-1 rounded-full text-stock-up bg-current"
              style={{ left: `${resistancePct}%` }}
              title={`阻力目标位: ${safeFixed(resistancePrice, 2)}`}
            />
          </div>

          {/* 标尺数值对照 */}
          <div className="flex justify-between text-[12px] font-mono pt-2 text-muted-foreground">
            <div className="text-stock-down">
              <div>防守 {safeFixed(supportPrice, 2)}</div>
              {currentPrice != null && currentPrice > 0 ? (
                <div className="text-[12px] text-muted-foreground">
                  {safeFixed(((supportPrice - currentPrice) / currentPrice) * 100, 1)}%
                </div>
              ) : null}
            </div>
            {currentPrice != null ? (
              <div className="text-role-watch font-bold text-center">
                <div>现价 {safeFixed(currentPrice, 2)}</div>
                <div className="text-[12px] text-muted-foreground">基准价</div>
              </div>
            ) : null}
            {costPrice != null ? (
              <div className="text-amber-500 text-center">
                <div>成本 {safeFixed(costPrice, 2)}</div>
                <div className="text-[12px] text-muted-foreground">保本位</div>
              </div>
            ) : null}
            <div className="text-stock-up text-right">
              <div>阻力 {safeFixed(resistancePrice, 2)}</div>
              {currentPrice != null && currentPrice > 0 ? (
                <div className="text-[12px] text-muted-foreground">
                  +{safeFixed(((resistancePrice - currentPrice) / currentPrice) * 100, 1)}%
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>

      {/* 战术操盘预案 */}
      {tactics && tactics.length > 0 && (
        <div className="p-3 bg-s2 space-y-1.5 text-[12px] text-foreground border-b border-border">
          <div className="font-semibold text-foreground">操盘战术预案：</div>
          <ul className="space-y-1 text-[12px] leading-relaxed">
            {tactics.map((tactic, idx) => (
              <li key={idx} className="flex items-start gap-1.5">
                <span className="font-bold text-role-watch">{idx + 1}.</span>
                <span>{tactic}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 底部快捷操作 */}
      <div className="flex flex-wrap items-center justify-between bg-s3 p-2.5 gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {onDrawOnChart && (
            <button
              type="button"
              onClick={onDrawOnChart}
              className="flex items-center gap-1 rounded bg-role-watch/20 hover:bg-role-watch/30 px-2.5 py-1 text-[12px] font-medium text-role-watch transition"
            >
              <ArrowUpRight className="w-3.5 h-3.5" />
              在 K 线叠加画线
            </button>
          )}
          {onSetAlert && (
            <button
              type="button"
              onClick={() => onSetAlert(supportPrice)}
              className="flex items-center gap-1 rounded bg-s1 hover:bg-s2 px-2.5 py-1 text-[12px] text-foreground border border-border transition"
            >
              <Bell className="w-3.5 h-3.5 text-muted-foreground" />
              设 {safeFixed(supportPrice, 2)} 破位告警
            </button>
          )}
        </div>
        {onCopy && (
          <button
            type="button"
            onClick={onCopy}
            className="text-[12px] text-muted-foreground hover:text-foreground transition"
          >
            复制战报
          </button>
        )}
      </div>
    </div>
  )
}
